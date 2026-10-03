#!/usr/bin/env python3
"""Build genuine 1950–2025 continent and World assets without changing Europe.

uv run --with-requirements requirements-data.txt python scripts/build_data.py --regions
Compressed upstream annual files are staged in a fresh owned temporary directory;
only those files are removed after export. Existing source caches are never touched.
Manifests publish available years atomically; baseline.complete becomes true only
when all requested years have been exported. Resume recomputes exact pooled raw
histograms from exported frames, avoiding any source redownload for finished years.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gc
import gzip
import json
from pathlib import Path
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import netCDF4
import numpy as np
import requests
from scipy.ndimage import gaussian_filter
import shapefile
import shapely
from shapely.geometry import box, mapping, shape
from shapely.geometry.polygon import orient

from build_data import (NATURAL_EARTH_URL, NODATA, TEMPERATURE_PERCENTILES,
                        PRECIPITATION_PERCENTILES, EQUAL_PERCENTILES, acquire,
                        digest, encode_fields, export_binary, histogram_percentiles,
                        json_value, source_record, write_json)

CATALOG = 'https://thredds.northwestknowledge.net/thredds/catalog/TERRACLIMATE_ALL/data/catalog.xml'
FILE_BASE = 'https://thredds.northwestknowledge.net/thredds/fileServer/TERRACLIMATE_ALL/data/'
DESCRIPTOR_BASE = 'https://thredds.northwestknowledge.net/thredds/ncss/grid/TERRACLIMATE_ALL/data/'
REGIONS = [
    ('europe', 'Europe', 'European', [-17, -55], [('Reykjavík',-21.94,64.15),('Lisbon',-9.14,38.72),('Madrid',-3.70,40.42),('London',-.13,51.51),('Paris',2.35,48.86),('Bergen',5.32,60.39),('Oslo',10.75,59.91),('Stockholm',18.07,59.33),('Helsinki',24.94,60.17),('Berlin',13.40,52.52),('Zürich',8.54,47.38),('Rome',12.50,41.90),('Athens',23.73,37.98),('Warsaw',21.01,52.23),('Kyiv',30.52,50.45),('Bucharest',26.10,44.43),('Moscow',37.62,55.75)]),
    ('asia', 'Asia', 'Asian', [-95, -30], [('Tokyo',139.6917,35.6895),('Beijing',116.4074,39.9042),('Delhi',77.209,28.6139),('Mumbai',72.8777,19.076),('Bangkok',100.5018,13.7563),('Singapore',103.8198,1.3521),('Jakarta',106.8456,-6.2088),('Seoul',126.978,37.5665),('Dubai',55.2708,25.2048),('Istanbul',28.9784,41.0082),('Ulaanbaatar',106.9057,47.8864)]),
    ('africa', 'Africa', 'African', [-20,0], [('Cairo',31.2357,30.0444),('Lagos',3.3792,6.5244),('Nairobi',36.8219,-1.2921),('Cape Town',18.4241,-33.9249),('Johannesburg',28.0473,-26.2041),('Kinshasa',15.2663,-4.4419),('Casablanca',-7.5898,33.5731),('Accra',-.187,5.6037),('Addis Ababa',38.7578,8.9806),('Antananarivo',47.5079,-18.8792)]),
    ('north-america','North America','North American',[100,-45],[('New York',-74.006,40.7128),('Los Angeles',-118.2437,34.0522),('Mexico City',-99.1332,19.4326),('Toronto',-79.3832,43.6532),('Vancouver',-123.1207,49.2827),('Anchorage',-149.9003,61.2181),('Honolulu',-157.8583,21.3069),('Havana',-82.3666,23.1136),('Panama City',-79.5199,8.9824),('Nuuk',-51.7216,64.1835)]),
    ('south-america','South America','South American',[60,15],[('São Paulo',-46.6333,-23.5505),('Rio de Janeiro',-43.1729,-22.9068),('Buenos Aires',-58.3816,-34.6037),('Santiago',-70.6693,-33.4489),('Lima',-77.0428,-12.0464),('Bogotá',-74.0721,4.711),('Caracas',-66.9036,10.4806),('Quito',-78.4678,-.1807),('Manaus',-60.0217,-3.119),('Ushuaia',-68.303,-54.8019)]),
    ('oceania','Oceania','Oceanian',[-150,20],[('Sydney',151.2093,-33.8688),('Melbourne',144.9631,-37.8136),('Perth',115.8605,-31.9505),('Auckland',174.7633,-36.8485),('Wellington',174.7762,-41.2866),('Port Moresby',147.1803,-9.4438),('Suva',178.4501,-18.1248),('Apia',-171.7514,-13.8507),('Nukuʻalofa',-175.1982,-21.1394),('Papeete',-149.5585,-17.5516)]),
    ('antarctica','Antarctica','Antarctic',[0,90],[('McMurdo Station',166.6682,-77.8419),('Amundsen–Scott South Pole Station',0,-90),('Rothera Station',-68.125,-67.568),('Palmer Station',-64.053,-64.774),('Dumont d’Urville Station',140.001,-66.663),('Vostok Station',106.837,-78.464)]),
    ('world','World','World',[0,0],[('London',-.1276,51.5072),('New York',-74.006,40.7128),('São Paulo',-46.6333,-23.5505),('Cairo',31.2357,30.0444),('Nairobi',36.8219,-1.2921),('Tokyo',139.6917,35.6895),('Delhi',77.209,28.6139),('Sydney',151.2093,-33.8688),('Apia',-171.7514,-13.8507),('Anchorage',-149.9003,61.2181)]),
]


def annual_url(variable, year):
    return FILE_BASE + f'TerraClimate_{variable}_{year}.nc'


def metadata_evidence(output):
    """Persist actual time axes and CF packing for both endpoint years."""
    folder = output / 'regions' / 'provenance'
    folder.mkdir(parents=True, exist_ok=True)
    response = requests.get(CATALOG, timeout=(30,120)); response.raise_for_status()
    (folder / 'catalog.xml').write_bytes(response.content)
    evidence = {'catalog': source_record(CATALOG, folder/'catalog.xml'), 'annualDescriptors': {}}
    for year in (1950,2025):
        for variable in ('tmax','ppt'):
            name = f'TerraClimate_{variable}_{year}.nc'
            url = DESCRIPTOR_BASE + name + '/dataset.xml'
            r = requests.get(url,timeout=(30,120)); r.raise_for_status()
            target = folder / (name + '.dataset.xml'); target.write_bytes(r.content)
            root = ET.fromstring(r.content)
            axis = root.find("axis[@name='time']")
            attrs = {a.get('name'):a.get('value') for a in axis.findall('attribute')}
            numbers = np.array([float(x) for x in axis.find('values').text.split()])
            dates = [d.strftime('%Y-%m-%d') for d in netCDF4.num2date(numbers,attrs['units'],attrs.get('calendar','standard'))]
            if dates != [f'{year}-{m:02d}-01' for m in range(1,13)]:
                raise ValueError(f'Incomplete authoritative descriptor: {name}: {dates}')
            packing = {a.get('name'): a.get('value') for a in root.findall('gridSet/grid/attribute')}
            evidence['annualDescriptors'][f'{variable}/{year}'] = {**source_record(url,target),'dates':dates,'attributes':packing}
            print(f'Authoritative monthly coverage {variable}/{year}: {dates}',flush=True)
    write_json(folder/'source-availability.json',evidence)
    return evidence


def country_geometries(path):
    groups = {label: [] for _,label,_,_,_ in REGIONS if label not in ('Europe','World')}
    features = {label: [] for label in groups}; features['World'] = []
    all_geometry = []; version = None
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            if member.upper().endswith('.VERSION.TXT'):
                version = archive.read(member).decode().strip()
        with archive.open('ne_10m_admin_0_countries.shp') as shp, archive.open('ne_10m_admin_0_countries.shx') as shx, archive.open('ne_10m_admin_0_countries.dbf') as dbf:
            reader = shapefile.Reader(shp=shp,shx=shx,dbf=dbf,encoding='utf-8')
            for record in reader.iterShapeRecords():
                props = record.record.as_dict(); geometry = shape(record.shape.__geo_interface__)
                if not geometry.is_valid: geometry = shapely.make_valid(geometry)
                parts = [geometry] if geometry.geom_type == 'Polygon' else [p for p in geometry.geoms if p.geom_type == 'Polygon']
                if not parts: continue
                geometry = shapely.union_all(parts); all_geometry.append(geometry)
                display = geometry.simplify(.01,preserve_topology=True)
                display = orient(display,sign=1) if display.geom_type == 'Polygon' else shapely.MultiPolygon([orient(p,sign=1) for p in display.geoms])
                feature = {'type':'Feature','properties':{'name':props['NAME_EN'],'continent':props['CONTINENT'],'isoA3':props['ADM0_A3']},'geometry':mapping(display)}
                features['World'].append(feature)
                if props['CONTINENT'] in groups:
                    groups[props['CONTINENT']].append(geometry); features[props['CONTINENT']].append(feature)
                if props['ADM0_A3'] == 'RUS':
                    eastern = geometry.intersection(shapely.union_all([box(60,-90,180,90),box(-180,-90,-160,90)]))
                    if not eastern.is_empty:
                        groups['Asia'].append(eastern)
                        features['Asia'].append({'type':'Feature','properties':{'name':'Russia (east of 60°E, including antimeridian components)','continent':'Asia','isoA3':'RUS'},'geometry':mapping(shapely.orient_polygons(eastern.simplify(.01,preserve_topology=True)))})
    geometries = {label:shapely.union_all(g) for label,g in groups.items()}
    geometries['World'] = shapely.union_all(all_geometry)
    for f in features.values(): f.sort(key=lambda x:x['properties']['name'])
    return geometries,features,version


def grid_for_region(geometry, lon, lat, stride):
    """Native centers; rotate antimeridian columns into minimal contiguous interval."""
    global_rows = np.arange(len(lat))[::stride]
    global_columns = np.arange(len(lon))[::stride]
    if stride == 1:
        _,south,_,north = geometry.bounds
        global_rows = global_rows[(lat >= south)&(lat <= north)]
    sampled_lon = lon[global_columns]; sampled_lat = lat[global_rows]
    shapely.prepare(geometry)
    mask = np.empty((len(global_rows),len(global_columns)), dtype=bool)
    for first in range(0,len(global_rows),128):
        mask[first:first+128] = shapely.intersects_xy(geometry,sampled_lon[None,:],sampled_lat[first:first+128,None])
    if stride == 1:
        occupied = np.flatnonzero(mask.any(axis=0))
        if not occupied.size: raise ValueError('No native land-cell centers in Natural Earth geometry')
        gaps = np.diff(np.r_[occupied,occupied[0]+len(lon)])
        gap = int(np.argmax(gaps)); start = int(occupied[(gap+1)%len(occupied)])
        count = len(lon)-int(gaps[gap])+1
        column_positions = (start+np.arange(count))%len(lon)
        mask = mask[:,column_positions]; global_columns = column_positions
        rows = np.flatnonzero(mask.any(axis=1)); mask=mask[rows[0]:rows[-1]+1]
        global_rows=global_rows[rows[0]:rows[-1]+1]
    unwrapped_lon = np.rad2deg(np.unwrap(np.deg2rad(lon[global_columns])))
    indices = np.flatnonzero(mask.ravel()).astype('<u4')
    return {'rows':global_rows,'columns':global_columns,'lon':unwrapped_lon,'lat':lat[global_rows], 'mask':mask,'indices':indices,'stride':stride}


def aggregate(path, variable, year):
    """Decode one month at a time, limiting memory regardless of source chunking."""
    with netCDF4.Dataset(path) as dataset:
        field=dataset.variables[variable]; field.set_auto_maskandscale(True)
        time=dataset.variables['time']
        dates=[d.strftime('%Y-%m-%d') for d in netCDF4.num2date(time[:],time.units,getattr(time,'calendar','standard'))]
        if dates != [f'{year}-{m:02d}-01' for m in range(1,13)]: raise ValueError(f'{variable}/{year}: {dates}')
        if field.dimensions != ('time','lat','lon'): raise ValueError(f'Unexpected dimensions {field.dimensions}')
        lon=np.array(dataset.variables['lon'][:]); lat=np.array(dataset.variables['lat'][:])
        if (len(lat),len(lon)) != (4320,8640) or not np.allclose(np.diff(lon),1/24) or not np.allclose(np.diff(lat),-1/24):
            raise ValueError('Not the native global TerraClimate grid')
        total=np.zeros((len(lat),len(lon)),dtype=np.float64); complete=np.ones(total.shape,dtype=bool)
        for month in range(12):
            decoded=field[month,:,:]
            values=np.asarray(decoded.filled(np.nan),dtype=np.float64)
            finite=np.isfinite(values); complete &= finite
            total += np.where(finite,values,0)
            print(f'Aggregating {variable}/{year} month {month+1}',flush=True)
        if variable == 'tmax': total /= 12
        total[~complete]=np.nan
        details={'dates':dates,'months':12,'cfPacking':{k:json_value(getattr(field,k)) for k in ('scale_factor','add_offset','_FillValue','missing_value') if hasattr(field,k)},'units':getattr(field,'units',None),'description':getattr(field,'description',None),'globalAttributes':{k:json_value(dataset.getncattr(k)) for k in dataset.ncattrs()},'completeYearCellsBeforeRegionMask':int(complete.sum()),'aggregation':'arithmetic mean of 12 monthly maximum-temperature fields; no day weighting' if variable == 'tmax' else 'sum of 12 monthly accumulated precipitation fields'}
    return total,lon,lat,details


def asset_base(region,grid,features,natural_record,natural_version):
    rid,label,title,rotate,cities=region
    prefix=f'regions/{rid}'
    return {'width':len(grid['lon']),'height':len(grid['lat']),'west':float(grid['lon'][0]),'north':float(grid['lat'][0]),'dx':grid['stride']/24,'dy':grid['stride']/24,'storedCells':len(grid['indices']),'cellIndexFile':f'{prefix}/cells.bin.gz','borderFile':f'{prefix}/borders.geojson','temperatureLabel':'Annual mean monthly maximum temperature','temperatureUnits':'°C','precipitationLabel':'Annual accumulated precipitation','precipitationUnits':'mm','binary':{'byteOrder':'little-endian','type':'int16','layout':'sparse sorted native row-major cell indices, interleaved [tmax × 100, ppt mm]','nodata':NODATA,'bytesPerCell':4,'temperaturePrecisionC':.01,'precipitationPrecisionMm':1},'temperaturePercentiles':TEMPERATURE_PERCENTILES,'precipitationPercentiles':PRECIPITATION_PERCENTILES,'equalPercentiles':EQUAL_PERCENTILES,'scope':{'label':label,'continentFilter':'All Natural Earth countries' if rid=='world' else f'Natural Earth CONTINENT == {label}','selection':'Unsimplified Natural Earth 1:10m country-continent polygons, including all country components; this is not a physical Europe/Asia division. Russia is tagged Europe and appears in World; its existing European bbox remains unchanged. Overseas country components retain their parent country continent tag. Display borders include source-uncovered land.','countries':[f['properties']['name'] for f in features],'mask':'Both variables finite in all 12 months AND unsimplified selected land at grid-cell center; static sparse polygon cell map preserves annual missing cells.','landCells':len(grid['indices']),'borderSimplificationDegrees':.01,'borderWinding':'RFC7946 counterclockwise exterior rings','longitudeWrapping':'Columns ordered eastward across the minimal circular longitude interval; west and cell-center longitudes may be unwrapped past 180 degrees.','horizStride':grid['stride'],'nominalResolutionKm':28 if rid=='world' else 4},'smoothing':{'enabledByDefault':True,'method':'scipy.ndimage.gaussian_filter','sigmaCells':2,'edgeMode':'reflect','truncate':4.,'description':'Fill invalid/ocean/non-region cells with this region’s annual full-precision valid-field mean, Gaussian sigma2 on the region grid, then restore the original valid mask and quantize. World sigma2 is on the stride6 grid.','caveat':'Visual smoothing biases coastal and mask-edge values toward the region annual mean; inspection and thresholds use unsmoothed values.'},'sources':{'naturalEarth':{**natural_record,'dataset':'Natural Earth 1:10m Admin 0 Countries','version':natural_version,'license':'Public domain','homepage':'https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-admin-0-countries/','boundaries':'Natural Earth de facto country boundaries, not a statement on sovereignty.'}},'percentileMethod':'Fixed pooled encoded raw land-cell/year histogram, NumPy linear interpolation; equal cell-year weighting, not area weighted.','generation':{'script':'scripts/build_data.py --regions (scripts/build_regions.py)','generatorVersion':1,'scriptSha256':digest(Path(__file__)),'dependenciesSha256':digest(Path(__file__).resolve().parents[1]/'requirements-data.txt'),'acquisition':'Original compressed upstream HTTPServer annual files; only two source files staged per year in a newly owned temporary directory, removed after export; no existing cache deletion.','command':'uv run --with-requirements requirements-data.txt python scripts/build_data.py --regions'}}


def export_region(year,region,grid,base,tmax,ppt,sources,output):
    rid,label,_,_,cities=region
    t=tmax[np.ix_(grid['rows'],grid['columns'])]; p=ppt[np.ix_(grid['rows'],grid['columns'])]
    valid=grid['mask'] & np.isfinite(t) & np.isfinite(p)
    if not valid.any(): return {'year':year,'validCells':0,'sources':sources}
    directory=output/'regions'/rid/'years'/str(year); directory.mkdir(parents=True,exist_ok=True)
    prefix=f'regions/{rid}/years/{year}'
    raw=encode_fields(t,p,valid).reshape(-1,2)[grid['indices']]
    export_binary(directory/'climate.bin.gz',raw)
    means=[float(t[valid].mean()),float(p[valid].mean())]
    smooth_t=gaussian_filter(np.where(valid,t,means[0]),sigma=2,mode='reflect')
    smooth_p=gaussian_filter(np.where(valid,p,means[1]),sigma=2,mode='reflect')
    smooth=encode_fields(smooth_t,smooth_p,valid).reshape(-1,2)[grid['indices']]
    export_binary(directory/'smoothed.bin.gz',smooth)
    metadata={'year':year,'validCells':int(valid.sum()),'incompleteYearLandCells':int((grid['mask']&~valid).sum()),'rawFile':f'{prefix}/climate.bin.gz','smoothedFile':f'{prefix}/smoothed.bin.gz','metadataFile':f'{prefix}/metadata.json','temperatureRange':[float(raw[:,0][raw[:,0]!=NODATA].min()/100),float(raw[:,0][raw[:,0]!=NODATA].max()/100)],'precipitationRange':[int(raw[:,1][raw[:,0]!=NODATA].min()),int(raw[:,1][raw[:,0]!=NODATA].max())],'fullPrecisionTemperatureRange':[float(t[valid].min()),float(t[valid].max())],'fullPrecisionPrecipitationRange':[float(p[valid].min()),float(p[valid].max())],'meanFillTmaxC':means[0],'meanFillPptMm':means[1],'sources':sources,'assetSha256':{f'{prefix}/{name}':digest(directory/name) for name in ('climate.bin.gz','smoothed.bin.gz')}}
    samples=[]
    for name,longitude,latitude in cities:
        circular_distance=np.abs((grid['lon']-longitude+180)%360-180)
        col=int(circular_distance.argmin()); row=int(np.abs(grid['lat']-latitude).argmin()); ok=bool(valid[row,col])
        samples.append({'name':name,'requestedLongitude':longitude,'requestedLatitude':latitude,'longitude':float(grid['lon'][col]),'latitude':float(grid['lat'][row]),'row':row,'column':col,'valid':ok,'tmaxC':float(np.rint(t[row,col]*100)/100) if ok else None,'pptMm':int(np.rint(p[row,col])) if ok else None})
    metadata['representativeValues']=samples
    write_json(directory/'metadata.json',metadata)
    print(f'Exported {rid}/{year}: {int(valid.sum()):,} complete of {len(grid["indices"]):,} polygon cells',flush=True)
    return metadata


def publish(region,base,records,histograms,output,requested):
    rid,label,_,_,_=region
    covered={str(y):records[y] for y in sorted(records) if records[y]['validCells']}
    manifest=dict(base)
    manifest.update({'years':[int(y) for y in covered],'defaultYear':max(map(int,covered)) if covered else None,'frames':{y:{k:m[k] for k in ('year','validCells','rawFile','smoothedFile','metadataFile','temperatureRange','precipitationRange','fullPrecisionTemperatureRange','fullPrecisionPrecipitationRange')} for y,m in covered.items()},'baseline':{'startYear':1950,'endYear':2025,'requestedYears':requested,'completedSourceYears':list(records),'complete':len(records)==len(requested)},'coverageEvidence':{str(y):{'completeYearRegionCells':m['validCells'],'sourceMetadataFile':f'regions/provenance/{y}.json'} for y,m in records.items()},'annualMaskVariation':'Each year independently requires all12 finite monthly values of both variables; raw and smoothed share the same annual mask.'})
    manifest['assetSha256']={path:sha for m in covered.values() for path,sha in m['assetSha256'].items()}
    for name in ('cells.bin.gz','borders.geojson'):
        manifest['assetSha256'][f'regions/{rid}/{name}']=digest(output/'regions'/rid/name)
    manifest['totalCompressedBytes']=sum((output/p).stat().st_size for p in manifest['assetSha256'] if p.endswith('.gz'))
    if covered:
        latest=covered[str(manifest['defaultYear'])]
        manifest.update({k:latest[k] for k in ('validCells','rawFile','smoothedFile','metadataFile','temperatureRange','precipitationRange','representativeValues')})
        manifest['sources']={**base['sources'],'terraClimate':latest['sources']}
        manifest['pooledValidCellYears']=int(histograms[0].sum())
        manifest['thresholdReference']=f'Pooled annual {label} encoded raw land cells across available years {min(map(int,covered))}–{max(map(int,covered))}; final target 1950–2025'
        for name,hist,percentiles,scale in [('temperatureBreaks',histograms[0],TEMPERATURE_PERCENTILES,100),('precipitationBreaks',histograms[1],PRECIPITATION_PERCENTILES,1),('equalTemperatureBreaks',histograms[0],EQUAL_PERCENTILES,100),('equalPrecipitationBreaks',histograms[1],EQUAL_PERCENTILES,1)]: manifest[name]=histogram_percentiles(hist,percentiles,scale)
        manifest['temperatureRange']=histogram_percentiles(histograms[0],[0,100],100)
        manifest['precipitationRange']=histogram_percentiles(histograms[1],[0,100],1)
    else:
        manifest['coverageNote']=f'No cells with complete monthly tmax and ppt coverage in {label} in the actual downloaded source years {", ".join(map(str,records))}. Real Natural Earth outlines are shown; no raster or thresholds are fabricated.'
    if len(records)!=len(requested) and covered:
        manifest['coverageNote']=f'Acquisition in progress: {len(records)} of {len(requested)} source years processed; only completed genuine annual frames are offered. Final thresholds pool all 1950–2025.'
    write_json(output/'regions'/rid/'metadata.json',manifest)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-year',type=int,default=1950); parser.add_argument('--end-year',type=int,default=2025)
    parser.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parents[1]/'data')
    parser.add_argument('--staging-parent',type=Path,default=Path('/tmp'))
    args=parser.parse_args(argv)
    if not 1950<=args.start_year<=args.end_year<=2025: parser.error('Years must lie within 1950–2025')
    output=args.output_dir; (output/'regions').mkdir(parents=True,exist_ok=True)
    evidence=metadata_evidence(output)
    # Persist small geography/provenance assets, never reuse/delete the user cache.
    natural_path=output/'regions'/'provenance'/'ne_10m_admin_0_countries.zip'
    acquire(NATURAL_EARTH_URL,natural_path)
    natural_record=source_record(NATURAL_EARTH_URL,natural_path)
    geometries,features,natural_version=country_geometries(natural_path)
    registry={'defaultRegion':'europe','regions':[]}
    for rid,label,title,rotate,cities in REGIONS:
        item={'id':rid,'label':label,'title':title,'metadataFile':'metadata.json' if rid=='europe' else f'regions/{rid}/metadata.json','projection':{'type':'equalEarth' if rid=='world' else 'stereographic' if rid=='antarctica' else 'azimuthalEqualArea','rotate':rotate},'cities':cities}
        if rid=='antarctica': item['coverageNote']='Antarctic stations are geographic references, not cities. Annual coverage is determined from actual paired monthly TerraClimate source arrays.'
        registry['regions'].append(item)
    write_json(output/'regions'/'index.json',registry)
    lon=-179.97916666666666+np.arange(8640)/24; lat=89.97916666666667-np.arange(4320)/24
    states={}
    requested=list(range(args.start_year,args.end_year+1))
    # Latest and earliest source years first: provide source-based endpoint coverage promptly.
    order=list(dict.fromkeys([args.end_year,args.start_year,2024 if args.start_year<=2024<=args.end_year else args.start_year]+list(reversed(requested))))
    for region in REGIONS[1:]:
        rid,label,_,_,_=region; directory=output/'regions'/rid; directory.mkdir(parents=True,exist_ok=True)
        grid=grid_for_region(geometries[label],lon,lat,6 if rid=='world' else 1)
        export_binary(directory/'cells.bin.gz',grid['indices'])
        write_json(directory/'borders.geojson',{'type':'FeatureCollection','features':features[label]})
        base=asset_base(region,grid,features[label],natural_record,natural_version)
        base['scope']['selection'] = 'Unsimplified Natural Earth 1:10m country-continent polygons, including overseas country components tagged with their parent country. Asia additionally includes Russia east of 60°E and its eastern antimeridian components west of 160°W. Europe retains its original [-25,34,60,72] bbox and unchanged assets. European territory outside that bbox not allocated to eastern Russia (including Russia north of 72°N west of 60°E) is World-only. Continents therefore do not claim an exhaustive physical-geography partition. World includes all Natural Earth land and source-uncovered outlines.'
        if rid == 'asia':
            base['scope']['continentFilter'] = 'Natural Earth CONTINENT == Asia OR Russia east60°E/eastern antimeridian components'
        states[rid]={'region':region,'grid':grid,'base':base,'records':{},'hist':[np.zeros(65536,dtype=np.int64),np.zeros(65536,dtype=np.int64)]}
        print(f'Static {rid}: {len(grid["lon"])}×{len(grid["lat"])}; {len(grid["indices"]):,} sorted sparse cells',flush=True)
    staged=Path(tempfile.mkdtemp(prefix='bivariate-climate-regions-owned-',dir=args.staging_parent))
    print(f'Owned ephemeral staging: {staged}',flush=True)
    try:
        for year in order:
            provenance_path=output/'regions'/'provenance'/f'{year}.json'
            done=provenance_path.exists() and all((output/'regions'/rid/'years'/str(year)/'metadata.json').exists() or rid=='antarctica' and json.loads(provenance_path.read_text()).get('antarcticaCompleteYearCells')==0 for rid in states)
            if done:
                sources=json.loads(provenance_path.read_text()); arrays=None
                print(f'Resuming exported source year {year}',flush=True)
            else:
                paths={v:staged/f'TerraClimate_{v}_{year}.nc' for v in ('tmax','ppt')}
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(lambda v:acquire(annual_url(v,year),paths[v]),('tmax','ppt')))
                tmax,actual_lon,actual_lat,t_details=aggregate(paths['tmax'],'tmax',year)
                ppt,p_lon,p_lat,p_details=aggregate(paths['ppt'],'ppt',year)
                if not np.array_equal(actual_lon,p_lon) or not np.array_equal(actual_lat,p_lat): raise ValueError('Temperature and precipitation grids differ')
                if not np.allclose(actual_lon,lon,atol=1e-10) or not np.allclose(actual_lat,lat,atol=1e-10): raise ValueError('Unexpected source grid centers')
                arrays=(tmax,ppt)
                sources={'dataset':'TerraClimate','version':t_details['globalAttributes'].get('version'),'catalog':CATALOG,'homepage':'https://www.climatologylab.org/terraclimate.html','nativeResolutionDegrees':1/24,'license':t_details['globalAttributes'].get('license'),'citation':t_details['globalAttributes'].get('references'),'tmax':{**source_record(annual_url('tmax',year),paths['tmax']),**t_details},'ppt':{**source_record(annual_url('ppt',year),paths['ppt']),**p_details}}
            for rid,state in states.items():
                meta_path=output/'regions'/rid/'years'/str(year)/'metadata.json'
                if done and meta_path.exists(): metadata=json.loads(meta_path.read_text())
                elif done: metadata={'year':year,'validCells':0,'sources':sources}
                else: metadata=export_region(year,state['region'],state['grid'],state['base'],*arrays,sources,output)
                state['records'][year]=metadata
                if metadata['validCells']:
                    with gzip.open(output/metadata['rawFile'],'rb') as f: raw=np.frombuffer(f.read(),dtype='<i2').reshape(-1,2)
                    if len(raw)!=len(state['grid']['indices']): raise ValueError('Sparse exported size mismatch')
                    valid=raw[:,0]!=NODATA
                    for component,hist in enumerate(state['hist']): hist += np.bincount(raw[:,component][valid].astype(np.int32)-NODATA,minlength=65536)
                if rid=='antarctica': sources['antarcticaCompleteYearCells']=metadata['validCells']
                publish(state['region'],state['base'],state['records'],state['hist'],output,requested)
            write_json(provenance_path,sources)
            if arrays is not None:
                del arrays,tmax,ppt
                # This directory was freshly created by this invocation; nothing else is removed.
                for path in staged.iterdir(): path.unlink()
            gc.collect()
            print(f'Completed source year {year}; manifests now contain {len(states["world"]["records"])} of {len(requested)} source years',flush=True)
        print('All requested genuine source years exported; fixed pooled region thresholds complete.',flush=True)
    finally:
        shutil.rmtree(staged)


if __name__=='__main__': main()
