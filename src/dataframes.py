
# This script contains functions to read and process gridded and table outputs from the CAETE model.
# Author: Joao Paulo Darela Filho

from concurrent.futures import ProcessPoolExecutor as PoolExecutor

import os
from pathlib import Path
from typing import Union, Collection, Tuple, Dict, List

from numpy.typing import NDArray

import numpy as np
import pandas as pd

from caete import worker, region, grd_mt

from _geos import pan_amazon_region, get_region


# Get the region of interest
ymin, ymax, xmin, xmax = get_region(pan_amazon_region)

# IO
processed_data = Path("out_data")
model_results: Path = Path("./region_test_result.psz")
os.makedirs(processed_data, exist_ok=True)

# Load the region file
reg:region = worker.load_state_zstd(model_results)

# Variables to read
variables = ("rnpp", "npp", "cawood", "cue", "cfroot", "cleaf")


def get_args(variable: Union[str, Collection[str]])-> Collection[str]:
    if isinstance(variable, str):
        variable = [variable,]
    if isinstance(variable, Collection):
        return variable


def print_spins(r:region):
    """Prints the available spin slices for each gridcell in the region"""
    r[0].print_available_periods()


def print_variables(r:region):
    """Prints the available variables for each gridcell in the region"""
    reg[0]._get_daily_data("DUMMY", 1, pp=True)

#=========================================
# Functions dealing with gridded outputs
#=========================================
class gridded_data:

    @staticmethod
    def read_grd(grd:grd_mt,
                 variables: Union[str, Collection[str]],
                 spin_slice: Union[int, Tuple[int, int], None]
                 ) -> Tuple[NDArray, Union[Dict, NDArray, List, Tuple], Union[int, float], Union[int, float]]:
        """helper function to read gridcell output data.

        Args:
            grd (_type_): grd_mt
            variables (Collection[str]): which variables to read from the gridcell
            spin_slice (Union[int, Tuple[int, int], None]): which spin slice to read

        Returns:
            _type_: _description_
        """
        data = grd._get_daily_data(get_args(variables), spin_slice, return_time=True) # returns a tuple with data and time Tuple[NDArray, NDArray]
        time = data[-1]
        data = data[0]
        return time, data, grd.y, grd.x


    @staticmethod
    def aggregate_region_data(r: region,
                variables: Union[str, Collection[str]],
                spin_slice: Union[int, Tuple[int, int], None] = None
                )-> Dict[str, NDArray]:
        """_summary_

        Args:
            r (region): a region object

            variables (Union[str, Collection[str]]): variable names to read

            spin_slice (Union[int, Tuple[int, int], None], optional): which spin slice to read.
            Defaults to None, read all available data. Consumes a lot of memory.

        Returns:
            dict: a dict with the following keys: time, coord, data holding data to be transformed
            necessary to create masked arrays and subsequent netCDF files.
        """

        output = []
        nproc = min(len(r), r.nproc//2)
        nproc = max(1, nproc) # Ensure at least one process is used
        with PoolExecutor(max_workers=nproc) as executor:
            futures = [executor.submit(gridded_data.read_grd, grd, variables, spin_slice) for grd in r]
            for future in futures:
                output.append(future.result())

        # Finalize the data object
        raw_data = np.array(output, dtype=object)
        # Reoeganize resources
        time = raw_data[:,0][0] # We assume all slices have the same time, thus we get the first one
        coord = raw_data[:,2:4][:].astype(np.int64) # 2D matrix of coordinates (y(lat), x(lon))}
        data = raw_data[:,1][:]  # array of dicts, each dict has the variables as keys and the time series as values
        dim_names = ["time", "coord", "data"]
        return dict(zip(dim_names, (time, coord, data)))

    @staticmethod
    def create_masked_arrays(data: dict):
        """ Reads a dict generated by aggregate_region_data and reorganize the data
        as masked_arrays with shape=(time, lat, lon) for each variable

        Args:
            data (dict): a dict generated by aggregate_region_data

        Returns:
            _type_: a tuple with a list of masked_arrays (for each variable)
            and the time array.
        """
        time = data["time"]
        coords = data["coord"]
        variables = list(data["data"][0].keys()) # holds variable names being processed

        # Get the shapes of the arrays
        for gridcell in data["data"]:
            for var in variables:
                print(gridcell[var].shape)

        # dim = data["data"][0][variables[0]].shape

        # TODO: manage 2D and 3D arrays
        # assert len(dim) == 1, "Only 1D array allowed"
        # arrays_dict = data["data"][:]

        # # Read dtypes
        # dtypes = []
        # for var in variables:
        #     # We assume all gridcells have the same variables, thus we get the first one
        #     dtypes.append(arrays_dict[0][var].dtype)

        # arrays = []
        # for i, var in enumerate(variables):
        #     arrays.append(np.ma.masked_all(shape=(dim[0], 360, 720), dtype=dtypes[i]))

        # for i, var in enumerate(variables):
        #     for j in range(len(coords)):
        #         arrays[i][:, coords[j][0], coords[j][1]] = arrays_dict[j][var]
        # # Crop the arrays to the region of interest
        # arrays = [a[:, ymin:ymax, xmin:xmax] for a in arrays]

        # return arrays, time


# ======================================
# Functions dealing with table outputs
# ======================================
class table_data:

    @staticmethod
    def process_grd(grd, variables, spin_slice):
        d = grd._get_daily_data(get_args(variables), spin_slice, return_time=True)
        time = [t.strftime("%Y-%m-%d") for t in d[1]]  # type: ignore
        data = d[0]  # type: ignore
        fname = f"grd_{grd.x}_{grd.y}.csv"
        df = pd.DataFrame(data, index=time)
        # return df
        df.to_csv(processed_data / fname, index_label='day')


    @staticmethod
    def make_df(r: region,
                variables: Union[str, Collection[str]],
                spin_slice: Union[int, Tuple[int, int], None] = None
                ):
        out = []
        nproc = min(len(r), r.nproc//2)
        nproc = max(1, nproc) # Ensure at least one process is used
        with PoolExecutor(max_workers=nproc) as executor:
            futures = [executor.submit(table_data.process_grd, grd, variables, spin_slice) for grd in r]
            for future in futures:
                out.append(future.result())
        return out


    @staticmethod
    def make_df2(r:region,
                variables: Union[str, Collection[str]],
                spin_slice: Union[int, Tuple[int, int], None] = None
                ):

        for grd in r:
            d = grd._get_daily_data(get_args(variables), spin_slice, return_time=True)

            time = [t.strftime("%Y-%m-%d") for  t in d[1]] # type: ignore
            data = d[0] # type: ignore
            fname = f"grd_{grd.x}_{grd.y}.csv"
            df = pd.DataFrame(data, index=time)
            df.rename_axis('day', axis='index')
            df.to_csv(processed_data/fname, index_label='day')



def main():
    data = gridded_data.aggregate_region_data(reg, ("cawood", "rnpp"), 12)
    # data = gridded_data.aggregate_region_data(reg, variables, spin_slice=12)
    return data



if __name__ == "__main__":
    data = main()
    a = gridded_data.create_masked_arrays(data)
    table_data.make_df2(reg, variables, spin_slice=12)