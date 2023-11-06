"""
Build the STAC catalog for Oregon BLM and Washington St DNR forest stands. 

Label collections. One per agency-survey comb.

     1. BLM - Coos Bay
     2. BLM - Rogue Valley
     3. BLM - Lane County
     4. USFS - Blue Mountains
     5. USFS - Umpqua
     6. USFS - Willamette
     7. USFS - Gifford Pinchot
     8. USFS - Fremont-Winema
     9. USFS - Rogue  
    10. USFS - Umatilla
    11. USFS - Wenatchee
    12. WADNR  

Data collections
    1. NAIP
    2. 3DEP
    3. Landsat 8
    4. LIDAR
    5. Attribute data

Name convention
    labels: <uuid>_<year>_<agency-survey>-label
    datasets: 
        - <uuid>_<year>_<agency-survey>_<dataset>-cog
        - <uuid>_<year>_<agency-survey>_<dataset>-preview
"""

# %%
import re
import logging
import json
import datetime
from pathlib import Path

from pystac import (
    Extent, 
    SpatialExtent, 
    TemporalExtent,
    Item,
    Asset,
    MediaType,
    Catalog,
    Collection,
    CatalogType,
    Provider,
    Link
)
from pystac.extensions.eo import Band, EOExtension
from pystac.extensions.label import LabelExtension, LabelType, LabelClasses
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.pointcloud import PointcloudExtension, Schema, Statistic

import pandas as pd
import numpy as np
import geopandas as gpd
import rasterio
import pdal
from shapely import geometry

from gdstools import ConfigLoader, image_collection, multithreaded_execution

logging.basicConfig(filename='build_stac.log', encoding='utf-8', filemode='w', level=logging.INFO)

# %%
def bbox_to_json(bbox):
    """
    Generate GeoJSON geometry from bounding box.

    :param bbox: A list of four coordinates representing the bounding box in the order 
        [minx, miny, maxx, maxy].
    :type bbox: list
    :return: A GeoJSON geometry representing the bounding box.
    :rtype: dict
    """
    geom = geometry.box(*bbox, ccw=True)
    return json.loads(gpd.GeoSeries(geom).to_json())

# %%
def create_label_item(
    row: pd.Series,
    attr_dict: dict,
    crs='EPSG:4326'
):
    """
    Create a label item.

    bbox
    label_id
    geometry as json

    """
    # Read label data
    # label_path = Path(label_path)
    # label_data = gpd.read_file(label_path)
    label_id = f'{row.uuid}_{row.year}_{row.source.lower()}-label'
    bbox = list(row.geometry.bounds)
    label_date = datetime.datetime(int(row.year), 1, 1)

    if attr_dict['label_task'] in ['classification', 'segmentation']:
        label_classes = [
            LabelClasses.create(classes=attr_dict['label_classes'],
                                name=attr_dict['label_name'])
        ]
    else:
        label_classes = None

    label_data = gpd.GeoSeries(row.geometry, crs=crs)
    label_data = json.loads(label_data.to_json())
    label_data['features'][0]['properties'] = row[2:-2].to_dict()

    # Create label item
    label_item = Item(
        id=f'{label_id}',
        geometry=label_data,
        bbox=bbox,
        datetime=label_date,
        properties={}
    )

    if attr_dict['label_type'] == 'vector':
        label_type = LabelType.VECTOR
    elif attr_dict['label_type'] == 'raster':
        label_type = LabelType.RASTER

    label_ext = LabelExtension.ext(label_item, add_if_missing=True)
    label_ext.apply(
        label_description=attr_dict['label_description'], 
        label_type=label_type,
        label_properties=attr_dict['label_properties'],
        label_tasks=attr_dict['label_task'],
        label_classes=label_classes
    )

    # Add link to label data
    # idx = label_path.as_posix().split('/').index('labels')
    # subdir = '/'.join(label_path.as_posix().split('/')[idx:-1])
    # url = 'https://fbstac-stands.s3.amazonaws.com/data/' + subdir + '/' + label_path.name
    # label_ext.add_geojson_labels(href=url)

    return label_item, label_ext


def create_pc_item(
        filepath: str,
        crs: str,
        asset_path_url: str,
    ):
    """Creates a STAC Item from a point cloud file.
    Source: 
    https://github.com/stac-extensions/pointcloud/blob/main/examples/pdal-to-stac.py   

    :param filepath: Path to the point cloud file.
    :param crs: CRS of the point cloud.
    :param asset_path_url: URL path to the asset.
    :return: A STAC Item.
    """
    def capture_date(pdalinfo):
        year = pdalinfo['creation_year']
        day = pdalinfo['creation_doy']
        date = datetime.datetime(int(year), 1, 1) + datetime.timedelta(int(day) - 1 if int(day) > 1 else int(day))
        return date#.isoformat()+'Z'

    def convertGeometry(geom, srs):
        from osgeo import ogr
        from osgeo import osr
        in_ref = osr.SpatialReference()
        in_ref.SetFromUserInput(srs)
        out_ref = osr.SpatialReference()
        out_ref.SetFromUserInput(crs)

        g = ogr.CreateGeometryFromJson(json.dumps(geom))
        g.AssignSpatialReference(in_ref)
        g.TransformTo(out_ref)
        return json.loads(g.ExportToJson())

    def convertBBox(obj):
        output = []
        output.append(float(obj['minx']))
        output.append(float(obj['miny']))
        output.append(float(obj['minz']))
        output.append(float(obj['maxx']))
        output.append(float(obj['maxy']))
        output.append(float(obj['maxz']))
        return output

    filepath = Path(filepath)

    # Read lidar data
    r = pdal.Reader.las(filepath.as_posix())
    hb = pdal.Filter.hexbin()
    s = pdal.Filter.stats()
    i = pdal.Filter.info()

    pipeline: pdal.Pipeline = r | hb | s | i

    count = pipeline.execute()

    boundary = pipeline.metadata['metadata'][hb.type]
    stats = pipeline.metadata['metadata'][s.type]
    info = pipeline.metadata['metadata'][i.type]
    copc = pipeline.metadata['metadata'][r.type]

    pc_id = filepath.name.replace('.laz', '')

    try:
        geom = convertBBox(stats['bbox'][crs])
    except KeyError:
        logging.warning(f'No bbox found for {pc_id}. Using native bbox.')
        geom = stats['bbox']['native']

    bbox = geom['bbox']

    try:
        pc_date = capture_date(copc)
    except ValueError:
        logging.warning(f'No date found for {pc_id}. Using year from filename.')
        year = int(Path(filepath).stem.split('_')[1])
        pc_date = datetime.datetime(int(year), 1, 1)
    
    try:
        pc_geom = convertGeometry(
            boundary['boundary_json'],
            copc['comp_spatialreference']
        )
    except KeyError:
        logging.warning(f'No geometry found for {pc_id}. Using bbox.')
        pc_geom = geom['boundary']   

    density = boundary.get('avg_pt_per_sq_unit', 0)
    schemas = [Schema(x) for x in info['schema']['dimensions']]
    stastistics = [Statistic(x) for x in stats['statistic']]

    item = Item(
        id=pc_id,
        geometry=pc_geom,
        bbox=bbox,
        datetime=pc_date,
        properties={},
    )

    pc_ext = PointcloudExtension.ext(item, add_if_missing=True)
    pc_ext.apply(
        count=count, 
        type='lidar',
        encoding='laszip',
        density=density,
        schemas=schemas,
        statistics=stastistics
    )

    proj = ProjectionExtension.ext(item, add_if_missing=True)
    proj.apply(epsg=crs)

    asset = Asset(href=asset_path_url + filepath.name)
    item.add_asset('feature', asset)

    return item


def create_image_item(
    image_path: str,
    thumb_path: str,
    metadata_path: str,
    asset_path_url: str,
):
    """
    Create a STAC item.

    :param image_path: Path to local COG image.
    :type image_path: str
    :param thumb_path: Path to local thumbnail image.
    :type thumb_path: str
    :param metadata_path: Path to local metadata file.
    :type metadata_path: str
    :param asset_path_url: URL to the asset path. This is the access point where users can 
     download the catalog assets.
    :type asset_path_url: str
    :return: A STAC item.
    :rtype: pystac.Item
    """
    # Load image data
    image_path = Path(image_path)
    thumb_path = Path(thumb_path)
    with rasterio.open(image_path) as src:
        crs = src.crs
        transform = src.transform
        shape = src.shape
        bbox = list(src.bounds)

    # Load metadata
    with open(metadata_path) as f:
        metadata = json.load(f)

    # Collect image properties
    image_date = datetime.datetime.utcfromtimestamp(
        metadata['properties']['system:time_start']/1000)
    image_id = image_path.stem.replace('-cog', '')

    image_geom = bbox_to_json(bbox)
    image_bands = metadata['bands']

    # Create item
    item = Item(
        id=image_id,
        geometry=image_geom,
        bbox=bbox,
        datetime=image_date,
        properties={},
    )

    # Add bands and projection
    bands = [Band.create(name=b['id'], common_name=b.get('name'))
             for b in image_bands]
    eo = EOExtension.ext(item, add_if_missing=True)
    eo.apply(bands=bands)

    proj = ProjectionExtension.ext(item, add_if_missing=True)
    proj.apply(epsg=crs.to_epsg())
    proj.apply(transform=transform, epsg=crs.to_epsg(), shape=shape)

    # Add links to assets
    item.add_asset('image', Asset(href=asset_path_url +
                   image_path.name, media_type=MediaType.COG))
    # item.add_asset('metadata', pystac.Asset(href=github_url +
    #                metadata_path[3:], media_type=pystac.MediaType.JSON))
    item.add_asset('thumbnail', Asset(href=asset_path_url +
                   thumb_path.name, media_type=MediaType.PNG))

    return item


def paths_to_dict(paths_list, idx):
    """
    Create a dictionary from a list of paths.

    :param paths_list: A list of file paths.
    :type paths_list: list
    :param idx: The index of the path to use for the collection name.
    :type idx: int
    :return: A dictionary with the collection name as the key and a list of file paths as the value.
    :rtype: dict
    """
    _dict = {}
    for p in paths_list:
        plist = p.split('/')
        # expects name in format cellid_year_state_agency/dataset
        nameparts = Path(p).stem.split('_')
        collection = plist[idx + 1]
        year = nameparts[1]
        if not re.match(r'^\d+$', year):
            _dict.setdefault(collection, []).append(p)
        else:
            _dict.setdefault(collection, {}).setdefault(year, []).append(p)
    return _dict


def build_stac(rootpath: Path, run_as: str = 'dev'):
    """
    Builds the Plot STAC (SpatioTemporal Asset Catalog).

    :param conf: A dictionary containing configuration information for the catalog.
    :type conf: Dict[str, Any]
    :param qq_shp: A GeoDataFrame containing the quad boundaries.
    :type qq_shp: gpd.GeoDataFrame
    :return: A STAC Catalog object.
    :rtype: Catalog
    """
    # Load config file
    logging.info('Loading config file')
    conf = ConfigLoader(rootpath).load()
    if run_as == 'dev':
        PLOTS = Path(conf.DEV_PLOTS)
        DATADIR = Path(conf.DEV_DATADIR)
        PLOTATTRS = Path(conf.DEV_PLOTATTRS)
    else:
        PLOTS = conf.DEV_PLOTS
        DATADIR = Path(conf.DATADIR)
        PLOTATTRS = Path(conf.PLOTATTRS)

    logging.info('Running build_stac.py as %s', run_as)
    logging.info('Reading plot shapefile from %s', PLOTS)
    logging.info('Reading plot attributes from %s', PLOTATTRS)
    logging.info('Loading data from %s', DATADIR)

    plots_shp = gpd.read_file(PLOTS)
    logging.info('Loaded plot shapefile with shape: %s', plots_shp.shape)
    
    # Build catalog
    fbench = Catalog(
        id='fbstac-plots',
        description='A STAC implementation for forest benchmarking and modeling',
        title='Forest Benchmarking and Modeling STAC',
    )

    today = datetime.datetime.today()
    datasets = image_collection(DATADIR) + image_collection(DATADIR, file_pattern='*.laz')
    images_dict = paths_to_dict(datasets, 6)
    attrs = pd.read_csv(PLOTATTRS)
    attrs = attrs[attrs.year <= today.year]

    # Drop tree species columns
    attrs.drop(attrs.columns[18:], axis=1, inplace=True)
    merged = attrs.merge(plots_shp[['uuid', 'source', 'geometry']], on='uuid')
    merged = gpd.GeoDataFrame(merged, crs=plots_shp.crs, geometry=merged.geometry)

    if run_as == 'dev':
        allimg = []
        for ds in images_dict: 
            sbs = images_dict[ds]
            if isinstance(sbs, dict):
                for y in sbs: 
                    allimg.append(sbs[y])
            else:
                allimg.append(sbs)

        img_uuids = {Path(item).name.split('_')[0] for sublist in allimg for item in sublist}
        merged = merged[merged.uuid.isin(img_uuids)]

    logging.info(f'{len(datasets)} images and {len(images_dict)} datasets found: {list(images_dict.keys())}')
    for dataset in images_dict:
        # Create one collection for each dataset
        dts_info = conf['items'][dataset]

        logging.info('Generating collection %s', dataset)
        providers = [
            Provider(
                name=p.get('name', None), 
                roles=[p.get('roles', None)], 
                url=p.get('url', None)
            )
            for p in dts_info['providers'].values()
        ]
        fbench_collection = Collection(
            id = f'{dataset}',
            description = dts_info['description'],
            providers = providers,
            license=dts_info['license']['type'],
            extent = {},
        )

        if dts_info['license']['url']:
            license_link = Link(
                rel='license', 
                target=dts_info['license']['url'],
                media_type='text/html'
            )
            fbench_collection.add_link(license_link)

        logging.info(f'Adding collection {dataset} to catalog')
        fbench.add_child(fbench_collection)

        dataset_paths = []
        if isinstance(images_dict[dataset], dict):
            logging.info(f'Dataset {dataset} contain images from multiple years.')
            for year in images_dict[dataset]:
                logging.info(f'Adding {len(images_dict[dataset][year])} images from {year}')
                dataset_paths.extend(images_dict[dataset][year])
            
        else:
            logging.info(f'Dataset {dataset} contain {len(images_dict[dataset])} images.')
            dataset_paths = images_dict[dataset]

    # def nearest_matching_paths(_dict, uuids, year, max_year_distance=2):
    #     _year = sorted([y for y in _dict.keys() if abs(int(y)-year) <= max_year_distance])
        
    #     if _year:
    #         return [p for p in _dict.get(_year[0]) if Path(p).name.split('_')[0] in uuids]
    #     else:
    #         return []

    # for survey, year in merged[['year', 'source']].groupby(
    #     ['source','year']).count().index.tolist():

    #     # Create and add items to collections
    #     # Create label collection
    #     uuids = merged.uuid[
    #         (merged.source == survey) & (merged.year == year)
    #     ].tolist()
        
    # for dataset in images_dict.keys():
    #     if isinstance(images_dict[dataset], dict):
    #         dataset_paths = nearest_matching_paths(images_dict[dataset], uuids, year)
    #         # print('\nAdding items to collection', dataset)
    #     else:
    #         dataset_paths = [p for p in images_dict[dataset] if Path(p).name.split('_')[0] in uuids]

        def add_item(image_path, collection):
            file_extension = Path(image_path).suffix
            idx = image_path.split('/').index(collection.id)
            subdir = '/'.join(image_path.split('/')[idx:-1])
            asset_path_url = 'https://fbstac-stands.s3.amazonaws.com/data/' + subdir + '/'

            if file_extension == '.laz':
                item = create_pc_item(image_path, 'EPSG:4326', asset_path_url)
            elif file_extension == '.tif':
                thumbnail_path = image_path.replace('-cog.tif', '-preview.png')
                metadata_path = image_path.replace(
                    '-cog.tif', '-metadata.json')
                item = create_image_item(
                    image_path, 
                    thumbnail_path, 
                    metadata_path, 
                    asset_path_url
                )
            else:
                raise ValueError(f'File extension {file_extension} not supported.')
            
            collection.add_item(item)
            return
        
        print(f'Found {len(dataset_paths)} items for {dataset}')
        collection = fbench.get_child(dataset)
        dataset_paths = [p for p in dataset_paths if Path(p).name.replace('-cog.tif', '') 
                            not in [i.id for i in collection.get_items()]]

        if dataset_paths:
            print(f'{len(dataset_paths)} new items will be added to {dataset}')
            logging.info(f'Adding items for collection {dataset}')

            params = [
                {
                    'image_path': image_path,
                    'collection': collection,
                }
                for image_path in dataset_paths
            ]

            multithreaded_execution(add_item, params)

            datetimes = [item.datetime for item in collection.get_all_items()]

            col_uuids = [Path(p).stem.split('_')[0] for p in dataset_paths]
            aoi = merged[merged.uuid.isin(col_uuids)].total_bounds.tolist()
            start_datetime = min(datetimes)
            end_datetime = max(datetimes)
            # for collection in fbench.get_all_collections():     
            #     if collection.id == dataset:
            collection.extent = Extent(
                spatial = SpatialExtent(aoi),
                temporal = TemporalExtent([[start_datetime, end_datetime]])
            )
        
        else:
            print(f'No new items will be added to {dataset}')

    # Create labels and add references to source items.
    # logging.info(f'Creating {survey} label and adding items and links to assets')
    for survey in merged.source.unique():

        logging.info(f'Creating {survey} label collection.')
        start_datetime = datetime.datetime(int(year), 1, 1)
        end_datetime = datetime.datetime(int(year), 12, 31)

        aoi = merged[merged.source == survey].total_bounds.tolist()
        label_info = conf['labels'][survey]
        label_info.update({'label_date': start_datetime})
        label_collection = Collection(
            id=f'{survey.lower()}-plots',
            description = label_info['description'],
            license = label_info['label_license'],
            providers= [
                Provider(
                    name=label_info['provider_name'], 
                    roles=label_info['provider_roles'], 
                    url=label_info['provider_url']
                )
            ],
            extent = Extent(
                SpatialExtent(aoi),
                TemporalExtent([[start_datetime, end_datetime]])
            )
        )

        if label_info['label_license']['url']:
            label_link = Link(
                rel = "license", 
                target = label_info['label_license']['url'],
                media_type = "text/html"
            )
            label_collection.add_link(label_link)

        def add_label_item(row, label_info):
            label_item, label_ext = create_label_item(row, label_info)
            label_uuid = label_item.id.split('_')[0]
            year = label_item.datetime.year

            logging.info(f'Adding items to label {label_item.id}')
            for k in images_dict.keys():
                collection = fbench.get_child(k)

                sel_items = []
                if isinstance(images_dict[k], dict):
                    _years = sorted([int(y) for y in images_dict[k].keys() if abs(int(y)-year) <= 2])

                    if _years:
                        sel_items = [item for item in collection.get_items() if item.id.split('_')[0] == label_uuid]
                        sel_items = [item for item in sel_items if item.datetime.year in _years]
                        if sel_items:
                            _year = min([item.datetime.year for item in sel_items], key=lambda x: abs(x-year))
                            sel_items = [item for item in sel_items if item.datetime.year == _year]
                            if '008dc9d1_2016_wa-western' in [item.id for item in sel_items]:
                                break
                        
                        del _years

                else:
                    sel_items = [item for item in collection.get_items() if item.id.split('_')[0] == label_uuid]
         
                if sel_items:
                    logging.info(f"\tSelected {k} item:{[item.id for item in sel_items]}") 
                    # add items
                    [
                        label_ext.add_source(item, assets=[list(item.assets.keys())[0]]) for item in sel_items
                    ]
                else:
                    logging.info(f"\tNo items found for {k} in {year} +/- 2 years.")
            
            label_collection.add_item(label_item)
            return

        sbs = merged[(merged.source == survey)]

        params = [
            {
                'row': row,
                'label_info': label_info,
            }
            for _, row in sbs.iterrows()
        ]

        # add_label_item(**params[0])
        multithreaded_execution(add_label_item, params)

        datetimes = [item.datetime for item in label_collection.get_all_items()]
        aoi = sbs.total_bounds.tolist()
        start_datetime = min(datetimes)
        end_datetime = max(datetimes)
        # for collection in fbench.get_all_collections():     
        #     if collection.id == dataset:
        label_collection.extent = Extent(
            spatial = SpatialExtent(aoi),
            temporal = TemporalExtent([[start_datetime, end_datetime]])
        )

        print('\nAdding label collection to catalog')
        fbench.add_child(label_collection)

    fbench.normalize_hrefs('fbstac')
    fbench.validate()
    logging.info('Validation complete')
    return fbench

if __name__ == '__main__':

    TARGET = '/home/ygalvan/stac/fbstac-plots'
    rootpath = Path(__file__).parent
    conf = ConfigLoader(rootpath).load()

    fb = build_stac(rootpath, run_as='dev')
    
    # Save catalog
    print('Saving catalog to', TARGET)
    Path(TARGET).mkdir(parents=True, exist_ok=True)
    fb.save(catalog_type=CatalogType.SELF_CONTAINED,
                dest_href=TARGET)
    print('Done!')
