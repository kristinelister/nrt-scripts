from __future__ import unicode_literals

import fiona
import os
import logging
import sys
import urllib
from datetime import datetime, timedelta
from collections import OrderedDict
import cartosql
import zipfile

# Constants
DATA_DIR = 'data'
SOURCE_URL = 'ftp://satepsanone.nesdis.noaa.gov/FIRE/HMS/GIS/hms_smoke{date}.zip'
SOURCE_URL_ARCHIVE = 'ftp://satepsanone.nesdis.noaa.gov/FIRE/HMS/GIS/ARCHIVE/hms_smoke{date}.zip'
FILENAME = 'hms_smoke{date}'
TIMESTEP = {'days': 1}
DATE_FORMAT = '%Y%m%d'
DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
CLEAR_TABLE_FIRST = False
LOG_LEVEL = logging.INFO
MAXAGE_UPLOAD = datetime.today() - timedelta(days=360)
MAX_CHECK_CURRENT = datetime.today() - timedelta(days=14)

# asserting table structure rather than reading from input
CARTO_TABLE = 'cli_037_smoke_plumes'
CARTO_SCHEMA = OrderedDict([
    ('the_geom', 'geometry'),
    ('_UID', 'text'),
    ('date', 'timestamp'),
    ('Satellite', 'text'),
    ('_start', 'timestamp'),
    ('_end', 'timestamp'),
    ('duration', 'text'),
    ('Density', 'numeric')
])
UID_FIELD = '_UID'
TIME_FIELD = 'date'

MAXROWS = 100000
MAXAGE = datetime.today() - timedelta(days=365*10)

# Generate UID
def genUID(date, pos_in_shp):
    return str('{}_{}'.format(date, pos_in_shp))

def getDate(uid):
    '''first 8 chr of ID'''
    return uid.split('_')[0]

def formatObservationDatetime(start, end, datetime_format=DATETIME_FORMAT):
    date, time = start.split(' ')
    year = int(date[:4])
    day = int(date[4:])-1 # Account for fact that we're initiating from day 1
    hour = int(time[:-2])
    minute = int(time[-2:])
    start_dt = datetime(year=year,month=1,day=1) + timedelta(days=day, hours=hour, minutes=minute)

    date, time = end.split(' ')
    year = int(date[:4])
    day = int(date[4:])-1 # Account for fact that we're initiating from day 1
    hour = int(time[:-2])
    minute = int(time[-2:])
    end_dt = datetime(year=year,month=1,day=1) + timedelta(days=day, hours=hour, minutes=minute)

    start = start_dt.strftime(datetime_format)
    end = end_dt.strftime(datetime_format)
    duration = str((end_dt - start_dt))
    return(start,end,duration)

def findShp(zfile):
    with zipfile.ZipFile(zfile) as z:
        for f in z.namelist():
            if os.path.splitext(f)[1] == '.shp':
                return f
    return False

def getNewDates(exclude_dates):
    '''Get new dates excluding existing'''
    new_dates = []
    date = datetime.today()
    while date > MAXAGE_UPLOAD:
        date -= timedelta(**TIMESTEP)
        datestr = date.strftime(DATE_FORMAT)
        logging.debug(datestr)
        if datestr not in exclude_dates:
            new_dates.append(datestr)
        else:
            logging.debug(datestr + "already in table")
    return new_dates


def processNewData(exclude_ids):
    new_ids = []

    # get non-existing dates
    dates = set([getDate(uid) for uid in exclude_ids])
    new_dates = getNewDates(dates)
    logging.debug(new_dates)

    for date in new_dates:
        tmpfile = '{}.zip'.format(os.path.join(DATA_DIR,
                                               FILENAME.format(date=date)))
        logging.info('Fetching {}'.format(date))

        ###
        ## First try the Archive, if not there, and less than 2 weeks old,
        ## try current folder
        ###

        try:
            url = SOURCE_URL_ARCHIVE.format(date=date)
            urllib.request.urlretrieve(url, tmpfile)
        except Exception as e:
            logging.warning('Could not retrieve {} from ARCHIVE'.format(url))
            logging.warning(e)
            if datetime.strptime(date, DATE_FORMAT) > MAX_CHECK_CURRENT:
                try:
                    url = SOURCE_URL.format(date=date)
                    urllib.request.urlretrieve(url, tmpfile)
                except Exception as e:
                    logging.warning('Could not retrieve {} from CURRENT'.format(url))
                    logging.error('This data was not found in either the ARCHIVE or CURRENT ftp folders, {}'.format(e))
                    continue
            else:
                continue

        # 2. Parse fetched data and generate unique ids
        logging.info('Parsing data')
        shpfile = '/{}'.format(findShp(tmpfile))
        zfile = 'zip://{}'.format(tmpfile)
        rows = []
        with fiona.open(shpfile, 'r', vfs=zfile) as shp:
            logging.debug(shp.schema)
            pos_in_shp = 0
            for obs in shp:
                start = obs['properties']['Start']
                end = obs['properties']['End']
                start, end, duration = formatObservationDatetime(start, end)

                obs['properties']['_start'] = start
                obs['properties']['_end'] = end
                obs['properties']['duration'] = duration

                uid = genUID(date, pos_in_shp)

                new_ids.append(uid)
                row = []
                for field in CARTO_SCHEMA.keys():
                    if field == 'the_geom':
                        row.append(obs['geometry'])
                    elif field == UID_FIELD:
                        row.append(uid)
                    elif field == TIME_FIELD:
                        row.append(date)
                    else:
                        row.append(obs['properties'][field])
                rows.append(row)
                pos_in_shp += 1
        # 3. Delete local files
        os.remove(tmpfile)

        # 4. Insert new observations
        new_count = len(rows)
        if new_count:
            logging.info('Pushing {} new rows'.format(new_count))
            cartosql.insertRows(CARTO_TABLE, CARTO_SCHEMA.keys(),
                                CARTO_SCHEMA.values(), rows)
    num_new = len(new_ids)
    return num_new


##############################################################
# General logic for Carto
# should be the same for most tabular datasets
##############################################################

def createTableWithIndices(table, schema, idField, otherFields=[]):
    '''Get existing ids or create table'''
    cartosql.createTable(table, schema)
    cartosql.createIndex(table, idField, unique=True)
    for field in otherFields:
        if field != idField:
            cartosql.createIndex(table, field, unique=False)

def getFieldAsList(table, field, orderBy=''):
    assert isinstance(field, str), 'Field must be a single string'
    r = cartosql.getFields(field, table, order='{}'.format(orderBy),
                           f='csv')
    return(r.text.split('\r\n')[1:-1])

def deleteExcessRows(table, max_rows, time_field, max_age=''):
    '''Delete excess rows by age or count'''
    num_dropped = 0
    if isinstance(max_age, datetime):
        max_age = max_age.isoformat()

    # 1. delete by age
    if max_age:
        r = cartosql.deleteRows(table, "{} < '{}'".format(time_field, max_age))
        num_dropped = r.json()['total_rows']

    # 2. get sorted ids (old->new)
    ids = getFieldAsList(CARTO_TABLE, 'cartodb_id', orderBy=''.format(TIME_FIELD))

    # 3. delete excess
    if len(ids) > max_rows:
        r = cartosql.deleteRowsByIDs(table, ids[:-max_rows])
        num_dropped += r.json()['total_rows']
    if num_dropped:
        logging.info('Dropped {} old rows from {}'.format(num_dropped, table))

def main():
    logging.basicConfig(stream=sys.stderr, level=LOG_LEVEL)
    logging.info('STARTING')

    if CLEAR_TABLE_FIRST:
        logging.info("Clearing table")
        cartosql.dropTable(CARTO_TABLE)

    # 1. Check if table exists and create table
    existing_ids = []
    if cartosql.tableExists(CARTO_TABLE):
        existing_ids = getFieldAsList(CARTO_TABLE, UID_FIELD)
    else:
        createTableWithIndices(CARTO_TABLE, CARTO_SCHEMA, UID_FIELD, otherFields=[TIME_FIELD])

    # 2. Iterively fetch, parse and post new data
    num_new = processNewData(existing_ids)
    logging.debug('Num new: {}'.format(num_new))
    existing_count = num_new + len(existing_ids)

    logging.info('Total rows: {}, New: {}, Max: {}'.format(
        existing_count, num_new, MAXROWS))

    # 3. Remove old observations
    deleteExcessRows(CARTO_TABLE, MAXROWS, TIME_FIELD, MAXAGE)

    logging.info('SUCCESS')
