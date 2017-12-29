import numpy as np
import os
import rasterio
import urllib2
import shutil
from contextlib import closing
from netCDF4 import Dataset
import datetime
import tinys3

def dataDownload(): 
    now = datetime.datetime.now()
    year = now.year
    month = now.month - 5

    print now
    print year
    print month

    remote_path = 'ftp://neoftp.sci.gsfc.nasa.gov/geotiff/MOD13A2_M_NDVI/'
    last_file = 'MOD13A2_M_NDVI_'+str(year)+'-'+"%02d" % (month,)+'.TIFF'

    local_path = os.getcwd()

    print remote_path
    print last_file
    print local_path

    with closing(urllib2.urlopen(remote_path+last_file)) as r:
        with open(last_file, 'wb') as f:
            shutil.copyfileobj(r, f)

    print local_path+'/'+last_file

    with rasterio.open(local_path+'/'+last_file) as src:
        npixels = src.width * src.height
        for i in src.indexes:
            band = src.read(i)
            print(i, band.min(), band.max(), band.sum()/npixels)

    
    return last_file

def tiffile(dst,outFile):
    
    
    CM_IN_FOOT = 30.48


    with rasterio.open(file) as src:
        kwargs = src.meta
        kwargs.update(
            driver='GTiff',
            dtype=rasterio.float64,  #rasterio.int16, rasterio.int32, rasterio.uint8,rasterio.uint16, rasterio.uint32, rasterio.float32, rasterio.float64
            count=1,
            compress='lzw',
            nodata=0,
            bigtiff='NO' 
        )

        windows = src.block_windows(1)

        with rasterio.open(outFile,'w',**kwargs) as dst:
            for idx, window in windows:
                src_data = src.read(1, window=window)

                src_data[src_data <= 0] = 0
                src_data[src_data >= 255] = 0

                dst_data = (src_data * CM_IN_FOOT).astype(rasterio.float64)
                dst.write_band(1, dst_data, window=window)

def s3Upload(outFile):
    # Push to Amazon S3 instance
    conn = tinys3.Connection(os.getenv('S3_ACCESS_KEY'),os.getenv('S3_SECRET_KEY'),tls=True)
    f = open(outFile,'rb')
    conn.upload(outFile,f,os.getenv('BUCKET'))

# Execution
now = datetime.datetime.now()
year = now.year
month = now.month - 4
outFile ='modis.tif'

print 'starting'
file = dataDownload()
print 'downloaded'
tiffile(file,outFile)
print 'converted'
s3Upload(outFile)
print 'finish'