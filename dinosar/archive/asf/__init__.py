"""Functions for querying ASF archive.

This module has utilities for querying the NASA Alaska Satellite Facility
Distributed Active Archive Center (`ASF DAAC`_). Designed to easily search for
Sentinel-1 SAR scenes load associated JSON metadata into a Geopandas Dataframe.

Notes
-----
This file contains library functions. To run as a script use::

    $ get_inventory_asf.py --help

.. _ASF DAAC:
   https://www.asf.alaska.edu/get-data/api/

"""

import requests
import json
import shapely.wkt
from shapely.geometry import box, mapping
import pandas as pd
import geopandas as gpd
import os
import subprocess
import sys


def load_asf_json(jsonfile: str):
    """Convert JSON metadata from ASF query to dataframe.

    JSON metadata returned from ASF DAAC API is loaded into a geopandas
    GeoDataFrame, with timestamps converted to datatime objects.

    Parameters
    ----------
    jsonfile : str
        Path to the json file from an ASF API query.

    Returns
    -------
    gf :  GeoDataFrame
        A geopandas GeoDataFrame

    """
    with open(jsonfile) as f:
        meta = json.load(f)[0]  # list of scene dictionaries

    df = pd.DataFrame(meta)
    polygons = df.stringFootprint.apply(shapely.wkt.loads)
    gf = gpd.GeoDataFrame(df,
                          crs={'init': 'epsg:4326'},
                          geometry=polygons)

    gf['timeStamp'] = pd.to_datetime(gf.sceneDate, format='%Y-%m-%d %H:%M:%S')
    gf['sceneDateString'] = gf.timeStamp.apply(
                                            lambda x: x.strftime('%Y-%m-%d'))
    gf['dateStamp'] = pd.to_datetime(gf.sceneDateString)
    gf['utc'] = gf.timeStamp.apply(lambda x: x.strftime('%H:%M:%S'))
    gf['orbitCode'] = gf.relativeOrbit.astype('category').cat.codes

    return gf


def summarize_orbits(gf):
    """Break inventory into separate dataframes by relative orbit.

    For each relative orbit in GeoDataFame, save simple summary of acquisition
    dates to acquisitions_[orbit].csv.

    Parameters
    ----------
    gf : GeoDataFrame
        a pandas geodataframe from load_asf_json

    """
    for orb in gf.relativeOrbit.unique():
        df = gf.query('relativeOrbit == @orb')
        gb = df.groupby('sceneDateString')
        nFrames = gb.granuleName.count()
        df = df.loc[:, ['sceneDateString', 'dateStamp', 'platform']]
        # Only keep one frame per date
        DF = df.drop_duplicates('sceneDateString').reset_index(drop=True)
        DF.sort_values('sceneDateString', inplace=True)
        DF.reset_index(inplace=True, drop=True)
        timeDeltas = DF.dateStamp.diff()
        DF['dt'] = timeDeltas.dt.days
        DF.loc[0, 'dt'] = 0
        DF['dt'] = DF.dt.astype('i2')
        DF['nFrames'] = nFrames.values
        DF.drop('dateStamp', axis=1, inplace=True)
        # DF.set_index('date') # convert to datetime difference
        DF.to_csv('acquisitions_{}.csv'.format(orb))


def save_geojson_footprints(gf):
    """Save all frames from each date as separate geojson file.

    JSON footprints with metadata are easily visualized if pushed to GitHub.
    This saves a bunch of [date].geojson files in local directory.

    Parameters
    ----------
    gf : GeoDataFrame
        a pandas geodataframe from load_asf_json

    """
    attributes = ('granuleName', 'downloadUrl', 'geometry')
    gb = gf.groupby(['relativeOrbit', 'sceneDateString'])
    S = gf.groupby('relativeOrbit').sceneDateString.unique()
    for orbit, dateList in S.iteritems():
        os.makedirs(orbit)
        for date in dateList:
            dftmp = gf.loc[gb.groups[(orbit, date)], attributes].reset_index(drop=True)
            outname = os.path.join(orbit, date+'.geojson')
            dftmp.to_file(outname, driver='GeoJSON')


def summarize_inventory(gf):
    """Get basic statistics for each track.

    For each relativeOrbit in the dataframe, return the first date, last date,
    number of dates, number of total frames, flight direction (ascending, or
    descending), and UTC observation time. Also calculates approximate archive
    size by assuming 5Gb * total frames. Prints results to screen and also
    saves inventory_summary.csv.

    Parameters
    ----------
    gf : GeoDataFrame
        a pandas geodataframe from load_asf_json

    """
    dfS = pd.DataFrame(index=gf.relativeOrbit.unique())
    dfS['Start'] = gf.groupby('relativeOrbit').sceneDateString.min()
    dfS['Stop'] = gf.groupby('relativeOrbit').sceneDateString.max()
    dfS['Dates'] = gf.groupby('relativeOrbit').sceneDateString.nunique()
    dfS['Frames'] = gf.groupby('relativeOrbit').sceneDateString.count()
    dfS['Direction'] = gf.groupby('relativeOrbit').flightDirection.first()
    dfS['UTC'] = gf.groupby('relativeOrbit').utc.first()
    dfS.sort_index(inplace=True, ascending=False)
    dfS.index.name = 'Orbit'
    dfS.to_csv('inventory_summary.csv')
    print(dfS)
    size = dfS.Frames.sum()*5 / 1e3
    print('Approximate Archive size = {} Tb'.format(size))


def merge_inventories(s1Afile, s1Bfile):
    """Merge Sentinel 1A and Sentinel 1B into single dataframe.

    ASF API queries are done per satellite, so queries for S1A and S1B need to
    be merged into a single dataframe.

    Parameters
    ----------
    s1Afile : str
        Path to the json file from an ASF API query for Sentinel-1A data.
    s1Bfile : str
        Path to the json file from an ASF API query for Sentinel-1B data.

    Returns
    -------
    gf :  GeoDataFrame
        A geopandas GeoDataFrame

    """
    print('Merging S1A and S1B inventories')
    gfA = load_asf_json(s1Afile)
    gfB = load_asf_json(s1Bfile)
    gf = pd.concat([gfA, gfB])
    gf.reset_index(inplace=True)

    return gf


def save_inventory(gf, outname='query.geojson', format='GeoJSON'):
    """Save inventory GeoDataFrame as a GeoJSON file.

    Parameters
    ----------
    gf : GeoDataFrame
        a pandas geodataframe from load_asf_json.
    outname : str
        name of output file.
    format : str
        OGR-recognized output format.

    """
    # WARNING: overwrites existing file
    if os.path.isfile(outname):
        os.remove(outname)
    # NOTE: can't save pandas Timestamps!
    # ValueError: Invalid field type <class 'pandas._libs.tslib.Timestamp'>
    gf.drop(['timeStamp', 'dateStamp'], axis=1, inplace=True)
    gf.to_file(outname, driver=format)
    print('Saved inventory: ', outname)


def download_scene(downloadUrl):
    """Download a granule from ASF.

    Launches an external `wget` command to download a single granule from ASF.

    Parameters
    ----------
    downloadUrl : str
        A valid download URL for an ASF granule.

    """
    print('Requires ~/.netrc file')
    cmd = 'wget -nc -c {downloadUrl}'
    print(cmd)
    # recommended way to launch external program
    # https://docs.python.org/3.6/library/subprocess.html#subprocess-replacements
    try:
        retcode = subprocess.call(cmd)
        if retcode < 0:
            print("Child was terminated by signal", -retcode, file=sys.stderr)
        else:
            print("Child returned", retcode, file=sys.stderr)
    except OSError as e:
        print("Execution failed:", e, file=sys.stderr)


def query_asf(snwe, sat='S1A', format='json'):
    """Search ASF with [south, north, west, east] bounds.

    Saves result to local file: query_{sat}.{format}

    Parameters
    ----------
    snwe : list
        bounding coordinates [south, north, west, east].
    sat : str
        satellite id (either 'S1A' or 'S1B')
    format : str
        output format of ASF API (json, csv, kml, metalink)

    Notes
    ----------
    API keywords = [absoluteOrbit,asfframe,maxBaselinePerp,minBaselinePerp,
    beamMode,beamSwath,collectionName,maxDoppler,minDoppler,maxFaradayRotation,
    minFaradayRotation,flightDirection,flightLine,frame,granule_list,
    maxInsarStackSize,minInsarStackSize,intersectsWith,lookDirection,
    offNadirAngle,output,platform,polarization,polygon,processingLevel,
    relativeOrbit,maxResults,processingDate,start or end acquisition time,
    slaveStart/slaveEnd

    """
    print(f'Querying ASF Vertex for Sentinel-{sat}...')
    miny, maxy, minx, maxx = snwe
    roi = shapely.geometry.box(minx, miny, maxx, maxy)
    polygonWKT = roi.to_wkt()

    baseurl = 'https://api.daac.asf.alaska.edu/services/search/param'
    # relativeOrbit=$ORBIT
    data = dict(intersectsWith=polygonWKT,
                platform=sat,
                processingLevel='SLC',
                beamMode='IW',
                output=format)

    r = requests.get(baseurl, params=data, timeout=100)
    print(r.url)
    # Save Directly to dataframe
    # df = pd.DataFrame(r.json()[0])
    with open(f'query_{sat}.{format}', 'w') as j:
        j.write(r.text)


def ogr2snwe(vectorFile, buffer=None):
    """Convert ogr shape to South,North,West,East bounds.

    Parameters
    ----------
    vectorFile : str
        path to OGR-recognized vector file.
    buffer : float
        Amount of buffer distance to add to shape (in decimal degrees).

    Returns
    -------
    snwe :  list
        a list of coorinate bounds [S, N, W, E]

    """
    gf = gpd.read_file(vectorFile)
    gf.to_crs(epsg=4326, inplace=True)
    poly = gf.geometry.convex_hull
    if buffer:
        poly = poly.buffer(buffer)
    W, S, E, N = poly.bounds.values[0]
    snwe = [S, N, W, E]

    return snwe


def snwe2file(snwe):
    """Use Shapely to convert to GeoJSON & WKT.

    Save local text files in variety of formats to record bounds: snwe.json,
    snwe.wkt, snwe.txt.

    Parameters
    ----------
    snwe : list
        bounding coordinates [south, north, west, east].

    """
    S, N, W, E = snwe
    roi = box(W, S, E, N)
    with open('snwe.json', 'w') as j:
        json.dump(mapping(roi), j)
    with open('snwe.wkt', 'w') as w:
        w.write(roi.to_wkt())
    with open('snwe.txt', 'w') as t:
        snweList = '[{0:.3f}, {1:.3f}, {2:.3f}, {3:.3f}]'.format(S, N, W, E)
        t.write(snweList)
