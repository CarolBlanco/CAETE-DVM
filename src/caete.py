# -*-coding:utf-8-*-
# "CAETÊ"
# Author:  João Paulo Darela Filho
"""
Copyright 2017- LabTerra

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
import os
import csv
import sys
from config import fortran_compiler_dlls

if sys.platform == "win32":
    try:
        os.add_dll_directory(fortran_compiler_dlls)
    except:
        raise ImportError("Could not add the DLL directory to the PATH")

from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from time import sleep
from typing import Union, Tuple, Dict, Callable, List

import bz2
import copy
import gc
import multiprocessing as mp
import pickle as pkl
import random as rd
import warnings

from joblib import dump, load
from numba import jit
from numba import float32 as f32

import cftime
import numpy as np

from hydro_caete import soil_water
from config import fetch_config
from _geos import find_indices_xy, find_indices, find_coordinates_xy, calculate_area
import metacommunity as mc
from parameters import tsoil, ssoil, hsoil, output_path
from output import budget_output

from caete_module import global_par as gp
from caete_module import budget as model
from caete_module import water as st
from caete_module import photo as m
from caete_module import soil_dec

# Global lock
lock = mp.Lock()

# Set warnings to default
warnings.simplefilter("default")

def rwarn(txt='RuntimeWarning'):
    warnings.warn(f"{txt}", RuntimeWarning)

def print_progress(iteration, total, prefix='', suffix='', decimals=2, bar_length=30):
    """FROM Stack Overflow/GIST, THANKS
    Call in a loop to create terminal progress bar

    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        bar_length  - Optional  : character length of bar (Int)
    """
    bar_utf = b'\xe2\x96\x88'  # bar -> unicode symbol = u'\u2588'
    str_format = "{0:." + str(decimals) + "f}"
    percents = str_format.format(100 * (iteration / float(total)))
    filled_length = int(round(bar_length * iteration / float(total)))
    bar = '█' * filled_length + '-' * (bar_length - filled_length)

    sys.stdout.write('\r%s |%s| %s%s %s' %
                     (prefix, bar, percents, '%', suffix)),

    if iteration == total:
        sys.stdout.write('\n')
    sys.stdout.flush()

def budget_daily_result(out):
    return budget_output(*out)

def catch_out_budget(out):
    # This is currently used in the ond implementation (classes grd and plot)
    # WARNING keep the lists of budget/carbon3 outputs updated with fortran code

    lst = ["evavg", "epavg", "phavg", "aravg", "nppavg",
           "laiavg", "rcavg", "f5avg", "rmavg", "rgavg", "cleafavg_pft", "cawoodavg_pft",
           "cfrootavg_pft", "stodbg", "ocpavg", "wueavg", "cueavg", "c_defavg", "vcmax",
           "specific_la", "nupt", "pupt", "litter_l", "cwd", "litter_fr", "npp2pay", "lnc",
           "limitation_status", "uptk_strat", 'cp', 'c_cost_cwm']

    return dict(zip(lst, out))

def catch_out_carbon3(out):
    lst = ['cs', 'snc', 'hr', 'nmin', 'pmin']

    return dict(zip(lst, out))

def str_or_path(fpath: Union[Path, str], check_exists:bool=True,
                check_is_dir:bool=False, check_is_file:bool=False) -> Path:

    """Converts fpath to a Path object if necessay, do some checks and return the Path object"""

    is_path = isinstance(fpath, (Path))
    is_str = isinstance(fpath, (str))
    is_str_or_path = is_str or is_path

    assert is_str_or_path, "fpath must be a string or a Path object"
    _val_ = fpath if is_path else Path(fpath)

    if check_exists:
        assert _val_.exists(), f"File/directory not found: {_val_}"
    if check_is_dir:
        assert not check_is_file, "Cannot check if a path is a file and a directory at the same time"
        assert _val_.is_dir(), f"Path is not a directory: {_val_}"
    if check_is_file:
        assert not check_is_dir, "Cannot check if a path is a file and a directory at the same time"
        assert _val_.is_file(), f"Path is not a file: {_val_}"

    return _val_

def get_co2_concentration(filename:Union[Path, str]):
    fname = str_or_path(filename, check_is_file=True)
    with open(fname, 'r') as file:
        # Use the Sniffer class to detect the dialect
        sample = file.read(1024)
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample)
        file.seek(0)
        data = list(csv.reader(file, dialect))
    if sniffer.has_header:
        data = data[1:]
    return dict(map(lambda x: (int(x[0]), float(x[1])), data))

def read_bz2_file(filepath:Union[Path, str]):
    fpath = str_or_path(filepath)
    with bz2.BZ2File(fpath, mode='r') as fh:
        data = pkl.load(fh)
    return data

def write_bz2_file(data:Dict, filepath:Union[Path, str]):
    fpath = str_or_path(filepath)
    with bz2.BZ2File(fpath, mode='wb') as fh:
        pkl.dump(data, fh)

def parse_date(date_string):
    formats = ['%Y%m%d', '%Y/%m/%d', '%Y-%m-%d', '%Y.%m.%d']
    for fmt in formats:
        try:
            dt = datetime.strptime(date_string, fmt)
            return cftime.real_datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        except ValueError:
            pass
    raise ValueError('No valid date format found')

@jit(nopython=True)
def neighbours_index(pos, matrix):
    neighbours = []
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0
    for i in range(max(0, pos[0] - 1), min(rows, pos[0] + 2)):
        for j in range(max(0, pos[1] - 1), min(cols, pos[1] + 2)):
            if (i, j) != pos:
                neighbours.append((i, j))
    return neighbours

@jit(nopython=True)
def inflate_array(nsize, partial, id_living):
    c = 0
    complete = np.zeros(nsize, dtype=f32)
    for n in id_living:
        complete[n] = partial[c]
        c += 1
    return complete

@jit(nopython=True)
def linear_func(temp, vpd, T_max=45, VPD_max=3):
    """Linear function to calculate the coupling between the atmosphere and the canopy"""
    linear_func = (temp / T_max + vpd / VPD_max) / 2.0

    # Ensure the output is between 0 and 1
    if linear_func > 1.0:
        linear_func = 1.0
    elif linear_func < 0.0:
        linear_func = 0.0

    linear_func = 0.0 if linear_func < 0.0 else linear_func
    linear_func = 1.0 if linear_func > 1.0 else linear_func

    return linear_func

@jit(nopython=True)
def atm_canopy_coupling(emaxm, evapm, air_temp, vpd):
    # Linear function
    omega = linear_func(air_temp, vpd)

    # Coupling
    coupling = emaxm * omega + evapm * (1 - omega)

    return coupling



class state_zero:
    """base class with input/output related data (paths, filenames, etc)
    """

    def __init__(self, y:Union[int, float], x:Union[int, float],
                output_dump_folder:str,
                get_main_table:Callable)->None:
        """Construct the basic gridcell object

        if you give a pair of integers, the gridcell will understand that you are giving the indices of the
        gridcell in a 2D numpy array of gridcells representing the area of simulation
        if you give a pair of floats, the gridcell will understand that you are giving the coordinates
        of the gridcell in the real world (latitude and longitude). The indices are used to locate
        the input file that contains the climatic and soil data. The files must be named as grd_x-y.pbz2 where x and y are the indices of the gridcell

        Args:

        y: int | float -> index in the 0 axis [zero-indexed] or geographic latitude coordinate [degrees North]
        x: int | float -> index in the 1 axis [zero-indexed] or geographic longitude coordinate [degrees East]
        output_dump_folder: str -> a string with a valid name to an output folder. This will be used to create a
        child directory in the output location for the region that contains this gridcell.

        """

        assert type(y) == type(x), "x and y must be of the same type"

        # Configuration data
        self.config = fetch_config("caete.toml")
        self.afex_config = self.config.fertilization


        # CRS
        self.yres = self.config.crs.yres
        self.xres = self.config.crs.xres

        self.y, self.x = find_indices_xy(N = y, W = x, res_y=self.yres,
                                         res_x=self.xres,rounding=2) if isinstance(x, float) else (y, x)

        self.lat, self.lon = find_coordinates_xy(self.y, self.x, res_y=self.yres,
                                                 res_x=self.xres) if isinstance(x, int) else (y, x)

        self.cell_area = calculate_area(self.lat, self.lon,
                                        dx=self.xres, dy=self.yres)

        # Files & IO
        self.xyname = f"{self.y}-{self.x}"
        self.grid_filename = f"gridcell{self.xyname}"
        self.input_fname = f"input_data_{self.xyname}.pbz2"
        self.input_fpath = None
        self.data = None

        # Name of the dump folder where this gridcell will dump model outputs. It is a child from ../outputs - defined in config.py
        self.plot_name = output_dump_folder

        # Plant life strategies table
        self.get_from_main_array = get_main_table
        self.ncomms = None
        self.metacomm = None

        self.realized_runs = []
        self.co2_data = None

        # OUTPUT FOLDER STRUCTURE
        self.outputs = {}       # dict, store filepaths of output data generated by this
        # Root dir for the region outputs
        self.out_dir = output_path/Path(output_dump_folder)
        os.makedirs(self.out_dir, exist_ok=True)
        self.flush_data = None

        # counts the execution of a time slice (a call of self.run_spinup)
        self.run_counter = 0


class climate:
    def __init__(self):
        self.pr = None
        self.ps = None
        self.rsds = None
        self.tas = None
        self.rhs = None

    def _set_clim(self, data:Dict):
        self.pr = data['pr']
        self.ps = data['ps']
        self.rsds = data['rsds']
        self.tas = data['tas']
        self.rhs = data['hurs']

    def _set_tas(self, data:Dict):
        self.tas = data['tas']
    def _set_pr(self, data:Dict):
        self.pr = data['pr']
    def _set_ps(self, data:Dict):
        self.ps = data['ps']
    def _set_rsds(self, data:Dict):
        self.rsds = data['rsds']
    def _set_rhs(self, data:Dict):
        self.rhs = data['hurs']
    def _set_co2(self, fpath:Union[Path, str]):
        self.co2_path = str_or_path(fpath, check_is_file=True)
        self.co2_data = get_co2_concentration(self.co2_path)


class time:
    def __init__(self):
        # Time attributes
        self.time_index:np.ndarray = None  # Array with the time indices
        self.calendar:str = None    # Calendar name
        self.time_unit:str = None   # Time unit
        self.start_date:str = None
        self.end_date:str = None
        self.ssize = None
        self.sind = None # Start index  of the time array
        self.eind = None # End index of the time array

    def _set_time(self, stime_i:Dict):
        self.stime = copy.deepcopy(stime_i)
        self.calendar = self.stime['calendar']
        self.time_index = self.stime['time_index']
        self.time_unit = self.stime['units']
        self.ssize = self.time_index.size
        self.sind = int(self.time_index[0])
        self.eind = int(self.time_index[-1])
        self.start_date = cftime.num2date(
            self.time_index[0], self.time_unit, calendar=self.calendar)
        self.end_date = cftime.num2date(
            self.time_index[-1], self.time_unit, calendar=self.calendar)


class soil:

    def __init__(self):

        # C,N & P
        self.sp_csoil = None
        self.sp_snc = None
        self.input_nut = None
        self.sp_available_p = None
        self.sp_available_n = None
        self.sp_so_n = None
        self.sp_in_n = None
        self.sp_so_p = None
        self.sp_in_p = None
        self.sp_csoil = None
        self.sp_snr = None
        self.sp_uptk_costs = None
        self.sp_organic_n = None
        self.sp_sorganic_n = None
        self.sp_organic_p = None
        self.sp_sorganic_p = None

        # Water
        # Water content for each soil layer
        self.wp_water_upper_mm = None  # mm
        self.wp_water_lower_mm = None  # mm
        self.wmax_mm = None  # mm
        self.theta_sat = None
        self.psi_sat = None
        self.soil_texture = None


    def _init_soil_cnp(self, data:Dict):
        self.sp_csoil = np.zeros(shape=(4,), order='F') + 0.001
        self.sp_snc = np.zeros(shape=(8,), order='F') + 0.0001
        self.input_nut = []
        self.nutlist = ['tn', 'tp', 'ap', 'ip', 'op']
        for nut in self.nutlist:
            self.input_nut.append(data[nut])
        self.soil_dict = dict(zip(self.nutlist, self.input_nut))
        self.sp_available_p = self.soil_dict['ap']
        self.sp_available_n = 0.2 * self.soil_dict['tn']
        self.sp_in_n = 0.4 * self.soil_dict['tn']
        self.sp_so_n = 0.2 * self.soil_dict['tn']
        self.sp_so_p = self.soil_dict['tp'] - sum(self.input_nut[2:])
        self.sp_in_p = self.soil_dict['ip']
        self.sp_organic_n = 0.1 * self.soil_dict['tn']
        self.sp_sorganic_n = 0.1 * self.soil_dict['tn']
        self.sp_organic_p = 0.5 * self.soil_dict['op']
        self.sp_sorganic_p = self.soil_dict['op'] - self.sp_organic_p


    def _init_soil_water(self, tsoil:Tuple, ssoil:Tuple, hsoil:Tuple):
        """Initializes the soil pools

        Args:
            tsoil (Tuple): tuple with the soil water content for the upper layer
            ssoil (Tuple): tuple with the soil water content for the lower layer
            hsoil (Tuple): tuple with the soil texture, saturation point and water potential at saturation
        """
        assert self.tas is not None, "Climate data not loaded"
        self.soil_temp = st.soil_temp_sub(self.tas[:1095] - 273.15)

        self.tsoil = []
        self.emaxm = []

        # GRIDCELL STATE
        # Water
        self.ws1 = tsoil[0][self.y, self.x].copy()
        self.fc1 = tsoil[1][self.y, self.x].copy()
        self.wp1 = tsoil[2][self.y, self.x].copy()

        self.ws2 = ssoil[0][self.y, self.x].copy()
        self.fc2 = ssoil[1][self.y, self.x].copy()
        self.wp2 = ssoil[2][self.y, self.x].copy()

        self.swp = soil_water(self.ws1, self.ws2, self.fc1, self.fc2, self.wp1, self.wp2)
        self.wp_water_upper_mm = self.swp.w1
        self.wp_water_lower_mm = self.swp.w2
        self.wmax_mm = np.float64(self.swp.w1_max + self.swp.w2_max)

        self.theta_sat = hsoil[0][self.y, self.x].copy()
        self.psi_sat = hsoil[1][self.y, self.x].copy()
        self.soil_texture = hsoil[2][self.y, self.x].copy()


    def add_soil_nutrients(self, afex_mode:str):
        if afex_mode == 'N':
            self.sp_available_n += self.afex_config["n"]
        elif afex_mode == 'P':
            self.sp_available_p += self.afex_config["p"]
        elif afex_mode == 'NP':
            self.sp_available_n += self.afex_config["n"]
            self.sp_available_p += self.afex_config["p"]


    def add_soil_water(self, data:Dict):
        # will deal with irrigation experiments and possibly water table depth
        pass


class gridcell_output:
    """Class to manage gridcell outputs
    """
    def __init__(self):
        self.flush_data = None
        self.soil_temp = None
        self.emaxm = None
        self.tsoil = None
        self.photo = None
        self.ls  = None
        self.aresp = None
        self.npp = None
        self.lai = None
        self.csoil = None
        self.inorg_n = None
        self.inorg_p = None
        self.sorbed_n = None
        self.sorbed_p = None
        self.snc = None
        self.hresp = None
        self.rcm = None
        self.f5 = None
        self.runom = None
        self.evapm = None
        self.wsoil = None
        self.swsoil = None
        self.rm = None
        self.rg = None
        self.cleaf = None
        self.cawood = None
        self.cfroot = None
        self.ocp_area = None
        self.wue = None
        self.cue = None
        self.cdef = None
        self.nmin = None
        self.pmin = None
        self.vcmax = None
        self.specific_la = None
        self.nupt = None
        self.pupt = None
        self.litter_l = None
        self.cwd = None
        self.litter_fr = None
        self.lnc = None
        self.storage_pool = None
        self.lim_status = None

        self.uptake_strategy = None
        self.carbon_costs = None


    def _allocate_output(self, n, npls, ncomms, save=True):
        """allocate space for the outputs
        n: int NUmber of days being simulated"""

        if not save:
            self.evapm = np.zeros(shape=(n,), order='F')
            self.runom = np.zeros(shape=(n,), order='F')
            self.nupt = np.zeros(shape=(2, n), order='F')
            self.pupt = np.zeros(shape=(3, n), order='F')
            self.litter_l = np.zeros(shape=(n,), order='F')
            self.cwd = np.zeros(shape=(n,), order='F')
            self.litter_fr = np.zeros(shape=(n,), order='F')
            self.lnc = np.zeros(shape=(6, n), order='F')
            self.storage_pool = np.zeros(shape=(3, n), order='F')
            self.ls = np.zeros(shape=(n,), order='F')
            return None

        # Daily outputs
        self.emaxm = []
        self.tsoil = []
        self.photo = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.aresp = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.npp = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))

        self.inorg_n = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.inorg_p = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.sorbed_n = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.sorbed_p = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.snc = np.zeros(shape=(8, n), order='F', dtype=np.dtype("float32"))
        self.hresp = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.rcm = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.f5 = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.runom = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.evapm = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.rm = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.rg = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.wue = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.cue = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.cdef = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.nmin = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.pmin = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.vcmax = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.nupt = np.zeros(shape=(2, n), order='F', dtype=np.dtype("float32"))
        self.pupt = np.zeros(shape=(3, n), order='F', dtype=np.dtype("float32"))
        self.litter_l = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.cwd = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.litter_fr = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.lnc = np.zeros(shape=(6, n), order='F', dtype=np.dtype("float32"))
        self.storage_pool = np.zeros(shape=(3, n), order='F', dtype=np.dtype("float32"))
        self.carbon_costs = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.ocp_area = np.zeros(shape=(npls, ncomms, n), dtype=('int32'), order='F')
        self.lim_status = np.zeros(
            shape=(3, npls, ncomms, n), dtype=np.dtype('int8'), order='F')
        self.uptake_strategy = np.zeros(
            shape=(2, npls, ncomms, n), dtype=np.dtype('int8'), order='F')

        # Annual outputs TODO:
        self.lai = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.csoil = np.zeros(shape=(4, n), order='F', dtype=np.dtype("float32"))
        self.wsoil = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.swsoil = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.cleaf = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.cawood = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.cfroot = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.specific_la = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))
        self.ls = np.zeros(shape=(n,), order='F', dtype=np.dtype("float32"))

    def _flush_output(self, run_descr, index):
        """1 - Clean variables that receive outputs from the fortran subroutines
           2 - Fill self.outputs dict with filepats of output data
           3 - Returns the output data to be writen

           runs_descr: str a name for the files
           index = tuple or list with the first and last values of the index time variable"""
        to_pickle = {}
        self.run_counter += 1
        if self.run_counter < 10:
            spiname = run_descr + "0" + str(self.run_counter) + out_ext
        else:
            spiname = run_descr + str(self.run_counter) + out_ext

        self.outputs[spiname] = os.path.join(self.out_dir, spiname)
        to_pickle = {'emaxm': np.array(self.emaxm),
                     "tsoil": np.array(self.tsoil),
                     "photo": self.photo,
                     "aresp": self.aresp,
                     'npp': self.npp,
                     'lai': self.lai,
                     'csoil': self.csoil,
                     'inorg_n': self.inorg_n,
                     'inorg_p': self.inorg_p,
                     'sorbed_n': self.sorbed_n,
                     'sorbed_p': self.sorbed_p,
                     'snc': self.snc,
                     'hresp': self.hresp,
                     'rcm': self.rcm,
                     'f5': self.f5,
                     'runom': self.runom,
                     'evapm': self.evapm,
                     'wsoil': self.wsoil,
                     'swsoil': self.swsoil,
                     'rm': self.rm,
                     'rg': self.rg,
                     'cleaf': self.cleaf,
                     'cawood': self.cawood,
                     'cfroot': self.cfroot,
                     'area': self.ocp_area,
                     'wue': self.wue,
                     'cue': self.cue,
                     'cdef': self.cdef,
                     'nmin': self.nmin,
                     'pmin': self.pmin,
                     'vcmax': self.vcmax,
                     'specific_la': self.specific_la,
                     'nupt': self.nupt,
                     'pupt': self.pupt,
                     'litter_l': self.litter_l,
                     'cwd': self.cwd,
                     'litter_fr': self.litter_fr,
                     'lnc': self.lnc,
                     'ls': self.ls,
                     'lim_status': self.lim_status,
                     'c_cost': self.carbon_costs,
                     'u_strat': self.uptake_strategy,
                     'storage_pool': self.storage_pool,
                     'calendar': self.calendar,    # Calendar name
                     'time_unit': self.time_unit,   # Time unit
                     'sind': index[0],
                     'eind': index[1]}
        # Flush attrs
        self.emaxm = []
        self.tsoil = []
        self.photo = None
        self.aresp = None
        self.npp = None
        self.lai = None
        self.csoil = None
        self.inorg_n = None
        self.inorg_p = None
        self.sorbed_n = None
        self.sorbed_p = None
        self.snc = None
        self.hresp = None
        self.rcm = None
        self.f5 = None
        self.runom = None
        self.evapm = None
        self.wsoil = None
        self.swsoil = None
        self.rm = None
        self.rg = None
        self.cleaf = None
        self.cawood = None
        self.cfroot = None
        self.area = None
        self.wue = None
        self.cue = None
        self.cdef = None
        self.nmin = None
        self.pmin = None
        self.vcmax = None
        self.specific_la = None
        self.nupt = None
        self.pupt = None
        self.litter_l = None
        self.cwd = None
        self.litter_fr = None
        self.lnc = None
        self.storage_pool = None
        self.ls = None
        self.lim_status = None
        self.carbon_costs = None,
        self.uptake_strategy = None

        return to_pickle


    def _save_output(self, data_obj):
        """Compress and save output data
        data_object: dict; the dict returned from _flush_output"""
        if self.run_counter < 10:
            fpath = "spin{}{}{}".format(0, self.run_counter, out_ext)
        else:
            fpath = "spin{}{}".format(self.run_counter, out_ext)
        with open(self.outputs[fpath], 'wb') as fh:
            dump(data_obj, fh, compress=('lz4', 6), protocol=4)
        self.flush_data = None


class grd_mt(state_zero, climate, time, soil, gridcell_output):

    """A gridcell object to run the model in the meta-community mode

    Args:
        base classes with climatic, soil data, and some common methods to manage gridcells
    """


    def __init__(self, y: int | float, x: int | float, data_dump_directory: str, get_main_table:Callable)->None:
        """Construct the gridcell object

        Args:
            y (int | float): latitude(float) or index(int) in the y dimension
            x (int | float): longitude(float) or index(int) in the x dimension
            data_dump_directory (str): Where this gridcell will dump model outputs
            get_main_table (callable): a region method used to get PLS from the main table
            to create the metacommunity.
        """

        super().__init__(y, x, data_dump_directory, get_main_table)


    def find_co2(self, year:int)->float:
        assert isinstance(year, int), "year must be an integer"
        assert self.co2_data, "CO2 data not loaded"
        _val_ = self.co2_data.get(year)
        if _val_ is None:
            raise ValueError(f"Year {year} not in ATM[CO₂] data")
        return _val_


    def find_index(self, start:int, end:int)->list:
        """_summary_

        Args:
            start (int): _description_
            end (int): _description_

        Raises:
            ValueError: _description_

        Returns:
            list: _description_
        """

        # Ensure start and end are within the bounds
        if start < self.sind or end > self.eind:
            raise ValueError("start or end out of bounds")

        # Directly find the indices
        start_index = np.where(np.arange(self.sind, self.eind + 1) == start)[0]
        end_index = np.where(np.arange(self.sind, self.eind + 1) == end)[0]

        # Combine and return the results
        return np.concatenate((start_index, end_index)).tolist()


    def change_input(self,
                    input_fpath:Union[Path, str]=None,
                    stime_i:Union[Dict, None]=None,
                    co2:Union[Dict, str, Path, None]=None)->None:
        """modify the input data for the gridcell

        Args:
            input_fpath (Union[Path, str], optional): _description_. Defaults to None.
            stime_i (Union[Dict, None], optional): _description_. Defaults to None.
            co2 (Union[Dict, str, Path, None], optional): _description_. Defaults to None.

        Returns:
            None: Changes the input data for the gridcell
        """
        #TODO: Add checks to ensure the data is time consistent
        if input_fpath is not None:
            #TODO prevent errors here
            self.input_fpath = Path(os.path.join(input_fpath, self.input_fname))
            assert self.input_fpath.exists()

            with bz2.BZ2File(self.input_fpath, mode='r') as fh:
                self.data = pkl.load(fh)

            self._set_clim(self.data)

        if stime_i is not None:
            self._set_time(stime_i)

        if co2 is not None:
            if isinstance(co2, (str, Path)):
                self._set_co2(co2)
            elif isinstance(co2, dict):
                self.co2_data = copy.deepcopy(co2)

        return None


    def set_gridcell(self,
                      input_fpath:Union[Path, str],
                      stime_i: Dict,
                      co2: Dict,
                      tsoil: Tuple[np.ndarray],
                      ssoil: Tuple[np.ndarray],
                      hsoil: Tuple[np.ndarray],
                      from_state: Union[bool, None]=None)->None:
        """ PREPARE A GRIDCELL TO RUN in the meta-community mode

        Args:
            input_fpath (Union[Path, str]): path to the input file with climatic and soil data
            stime_i (Dict): dictionary with the time index and units
            co2 (Dict): dictionary with the CO2 data
            pls_table (np.ndarray): np.array with the functional traits data
            tsoil (Tuple[np.ndarray]):
            ssoil (Tuple[np.ndarray]):
            hsoil (Tuple[np.ndarray]):
        """
        # Input data
        self.input_fpath = str_or_path(input_fpath)

        # # Meta-community
        # We want to run queues of gridcells in parallel. So each gridcell receives a copy of the PLS table object

        # Number of communities in the metacommunity. Defined in the config file {caete.toml}
        self.ncomms = self.config.metacomm.n  # Number of communities

        # Metacommunity object
        self.metacomm = mc.metacommunity(self.ncomms, self.get_from_main_array)


        # Read climate drivers and soil characteristics, incl. nutrients, for this gridcell
        # Having all data to one gridcell in a file enables to create/start the gricells in parallel (threading)
        # TODO: implement this multithreading in the region class to start all gridcells in parallel
        self.data = read_bz2_file(self.input_fpath)

        # Read climate data
        self._set_clim(self.data)

        # get CO2 data
        self.co2_data = copy.deepcopy(co2)

        # SOIL: NUTRIENTS and WATER
        self._init_soil_cnp(self.data)
        self._init_soil_water(tsoil, ssoil, hsoil)

        # TIME
        self._set_time(stime_i)

        return None


    def run_gridcell(self,
                  start_date,
                  end_date,
                  spinup=0,
                  fixed_co2_atm_conc=None,
                  save=True,
                  nutri_cycle=True,
                  afex=False,
                  reset_community=False,
                  kill_and_reset=False,
                  verbose=True):
        """ start_date [str]   "yyyymmdd" Start model execution

            end_date   [str]   "yyyymmdd" End model execution

            spinup     [int]   Number of repetitions in spinup. 0 for a transient run between start_date and end_date

            fix_co2    [Float] Fixed value for ATM [CO2]
                       [int]   Fixed value for ATM [CO2]
                       [str]   "yyyy" Corresponding year of an ATM [CO2]. Note that the text (csv/tsv) file with ATM [CO2]
                                data must have the year in the first column and the ATM [CO2] in the second column

            This function run the fortran subroutines and manage data flux. It
            is the proper CAETÊ-DVM execution in the start_date - end_date period, can be used for spinup or transient runs
        """

        assert not fixed_co2_atm_conc or isinstance(fixed_co2_atm_conc, str) or\
            fixed_co2_atm_conc > 0, "A fixed value for ATM[CO2] must be a positive number greater than zero or a proper string with the year - e.g., 'yyyy'"

        # Define start and end dates (read parameters)
        start = parse_date(start_date)
        end = parse_date(end_date)

        # Check dates sanity
        assert start < end, "Start date must be before end date"
        assert start >= self.start_date, "initial date out of bounds for the time array"
        assert end <= self.end_date, f"Final date out of bounds for the time array"


        # Define time index bounds for this run
        # During a run we are in general using a slice ov the available time span
        # to run the model. For example, we can run the model for a year or a decade
        # at the begining of the input data time series to spin up. This slice is defined
        # by the start and end dates provided in the arguments. HEre we get the indices.
        start_index = int(cftime.date2num(start, self.time_unit, self.calendar))
        end_index =   int(cftime.date2num(end, self.time_unit, self.calendar))

        # Find the indices in the time array [used to slice the timeseries with driver data  - tas, pr, etc.]
        lb, hb = self.find_index(start_index, end_index)

        # Define the time steps range
        # From zero to the last day of simulation
        steps = np.arange(lb, hb + 1)

        # Define the number of repetitions for the spinup
        spin = 1 if spinup == 0 else spinup

        # Define the AFEX mode
        afex_mode = self.afex_config["afex_mode"]

        # Slice&Catch climatic input and make conversions
        cv = self.config.conversion_factors_isimip

        temp = self.tas[lb: hb + 1] - cv.tas   # Air temp: model uses °C
        prec = self.pr[lb: hb + 1] * cv.pr     # Precipitation: model uses  mm/day
        p_atm = self.ps[lb: hb + 1] * cv.ps    # Atmospheric pressure: model uses hPa
        ipar = self.rsds[lb: hb + 1] * cv.rsds # PAR: model uses  mol(photons) m-2 s-1
        ru = self.rhs[lb: hb + 1] *  cv.rhs    # Relative humidity: model uses 0-1

        # Define the daily values for co2 concentrations
        co2_daily_values = np.zeros(steps.size, dtype=np.float32)

        if fixed_co2_atm_conc is None:
            # In this case, the co2 concentration will be updated daily.
            # We interpolate linearly between the yearly values of the atm co2 data
            co2 = self.find_co2(start.year)
            today = datetime(start.year, start.month, start.day, start.hour, start.minute, start.second)
            time_step = timedelta(days=1) # Define the time step
            today -= time_step # The first thing we do next is to add a day to the date. So we go back one day
            # Loop over the days and calculate the co2 concentration for each day
            for step in range(steps.size):
                today += time_step
                remaining = (datetime(today.year, 12, 31) - today).days + 1
                daily_fraction = (self.find_co2(today.year + 1) - co2) / (remaining + 1)
                co2 += daily_fraction
                co2_daily_values[step] = co2

        elif isinstance(fixed_co2_atm_conc, int) or isinstance(fixed_co2_atm_conc, float):
            # In this case, the co2 concentration will be fixed according to the numeric value provided in the argument
            co2 = fixed_co2_atm_conc
            co2_daily_values += co2
        elif isinstance(fixed_co2_atm_conc, str):
            # In this case, the co2 concentration will be fixed
            # According to the year provided in the argument
            # as a string. Format "yyyy".
            try:
                co2_year = int(fixed_co2_atm_conc)
            except ValueError:
                raise ValueError(
                    "The string(\"yyyy\") must be a number in the {self.start_date.year} - {self.end_date.year} interval")
            co2 = self.find_co2(co2_year)
            co2_daily_values += co2
        # self.co2_array = co2_daily_values

        # Start loops
        # THis outer loop is used to run the model for a number
        # of times defined by the spinup argument. THe model is
        # executed repeatedly between the start and end dates
        # provided in the arguments
        for s in range(spin):

            self._allocate_output(steps.size, self.metacomm.comm_npls, len(self.metacomm), save)

            # Loop over the days
            # Create a datetime object to track the dates
            today = datetime(start.year, start.month, start.day, start.hour, start.minute, start.second)
            # Calculate the number of days remaining in the year
            # remaining = (datetime(today.year, 12, 31) - today).days
            # Define the time step
            time_step = timedelta(days=1)
            # Go back one day
            today -= time_step

            for step in range(steps.size):
                today += time_step # Now it is today

                # Get the co2 concentration for the day
                co2 = co2_daily_values[step]
                # Update soil temperature
                self.soil_temp = st.soil_temp(self.soil_temp, temp[step])

                # AFEX
                if afex and today.timetuple().tm_yday == 364:
                    self.add_soil_nutrients(afex_mode)


                # Arrays to store values for each community in a simulated day
                xsize = len(self.metacomm)
                evavg = np.ma.masked_all(xsize, dtype=np.float32)
                epavg = np.ma.masked_all(xsize, dtype=np.float32)
                leaf_litter = np.ma.masked_all(xsize, dtype=np.float32)
                cwd = np.ma.masked_all(xsize, dtype=np.float32)
                root_litter = np.ma.masked_all(xsize, dtype=np.float32)
                lnc = np.ma.masked_all(shape=(6, xsize), dtype=np.float32)
                c_to_nfixers = np.ma.masked_all(xsize, dtype=np.float32)


                if save:
                    nupt = np.ma.masked_all(shape=(2, xsize), dtype=np.float32)
                    pupt = np.ma.masked_all(shape=(3, xsize), dtype=np.float32)
                    leaf_litter = np.ma.masked_all(xsize, dtype=np.float32)
                    c_to_nfixers = np.ma.masked_all(xsize, dtype=np.float32)
                    cwd = np.ma.masked_all(xsize, dtype=np.float32)
                    root_litter = np.ma.masked_all(xsize, dtype=np.float32)
                    lnc = np.ma.masked_all(shape=(6, xsize), dtype=np.float32)
                    cc = np.ma.masked_all(xsize, dtype=np.float32)
                    tsoil = np.ma.masked_all(xsize, dtype=np.float32)
                    photo = np.ma.masked_all(xsize, dtype=np.float32)
                    aresp = np.ma.masked_all(xsize, dtype=np.float32)
                    npp = np.ma.masked_all(xsize, dtype=np.float32)
                    lai = np.ma.masked_all(xsize, dtype=np.float32)
                    rcm = np.ma.masked_all(xsize, dtype=np.float32)
                    f5 = np.ma.masked_all(xsize, dtype=np.float32)
                    wsoil = np.ma.masked_all(xsize, dtype=np.float32)
                    rm = np.ma.masked_all(xsize, dtype=np.float32)
                    rg = np.ma.masked_all(xsize, dtype=np.float32)
                    cleaf = np.ma.masked_all(xsize, dtype=np.float32)
                    cawood = np.ma.masked_all(xsize, dtype=np.float32)
                    cfroot = np.ma.masked_all(xsize, dtype=np.float32)
                    wue = np.ma.masked_all(xsize, dtype=np.float32)
                    cue = np.ma.masked_all(xsize, dtype=np.float32)
                    cdef = np.ma.masked_all(xsize, dtype=np.float32)
                    vcmax = np.ma.masked_all(xsize, dtype=np.float32)
                    specific_la = np.ma.masked_all(xsize, dtype=np.float32)
                    storage_pool = np.ma.masked_all(shape=(3, xsize))
                    ocp_area = np.ma.masked_all(shape=(self.metacomm.comm_npls, xsize), dtype='int32')
                    lim_status = np.ma.masked_all(shape=(3, self.metacomm.comm_npls, xsize), dtype=np.dtype('int8'))
                    uptake_strategy = np.ma.masked_all(shape=(2, self.metacomm.comm_npls, xsize), dtype=np.dtype('int8'))

                # <- Daily loop indent level
                # Loop over communities
                sto =        np.zeros(shape=(3, self.metacomm.comm_npls), order='F')
                cleaf_in =   np.zeros(self.metacomm.comm_npls, order='F')
                cwood_in =   np.zeros(self.metacomm.comm_npls, order='F')
                croot_in =   np.zeros(self.metacomm.comm_npls, order='F')
                uptk_costs = np.zeros(self.metacomm.comm_npls, order='F')

                for i, community in enumerate(self.metacomm):
                    sto[0, :] = inflate_array(community.npls, community.vp_sto[0, :], community.vp_lsid)
                    sto[1, :] = inflate_array(community.npls, community.vp_sto[1, :], community.vp_lsid)
                    sto[2, :] = inflate_array(community.npls, community.vp_sto[2, :], community.vp_lsid)

                    cleaf_in[:] = inflate_array(community.npls, community.vp_cleaf, community.vp_lsid)
                    cwood_in[:] = inflate_array(community.npls, community.vp_cwood, community.vp_lsid)
                    croot_in[:] = inflate_array(community.npls, community.vp_croot, community.vp_lsid)
                    uptk_costs[:] = inflate_array(community.npls, community.sp_uptk_costs, community.vp_lsid)

                    ton = self.sp_organic_n #+ self.sp_sorganic_n
                    top = self.sp_organic_p #+ self.sp_sorganic_p

                    # Community daily budget calculation
                    out = model.daily_budget(community.pls_array, self.wp_water_upper_mm,
                                            self.wp_water_lower_mm, self.soil_temp, temp[step],
                                            p_atm[step], ipar[step], ru[step], self.sp_available_n,
                                            self.sp_available_p, ton, top, self.sp_organic_p,
                                            co2, sto, cleaf_in, cwood_in, croot_in, uptk_costs, self.wmax_mm)

                    # get daily budget results
                    daily_output = budget_daily_result(out)

                    # Update the community status
                    community.update_lsid(daily_output.ocpavg)
                    community.vp_ocp = daily_output.ocpavg[community.vp_lsid]
                    community.ls = community.vp_lsid.size
                    community.vp_cleaf = daily_output.cleafavg_pft[community.vp_lsid]
                    community.vp_cwood = daily_output.cawoodavg_pft[community.vp_lsid]
                    community.vp_croot = daily_output.cfrootavg_pft[community.vp_lsid]
                    community.vp_sto = daily_output.stodbg[:, community.vp_lsid]
                    community.sp_uptk_costs = daily_output.npp2pay[community.vp_lsid]

                    # Restore if it is the case or cycle if there is no PLS
                    if community.vp_lsid.size < 1:
                        if reset_community:
                            assert not save, "Cannot save data when resetting communities"
                            del daily_output
                            if verbose:
                                print(f"Reseting community {i}: Gridcell: {self.lat} °N, {self.lon} °E: In spin:{s}, step:{step}")
                            # Get the new life strategies. This is a method from the region class
                            with lock:
                                new_life_strategies = self.get_from_main_array(community.npls)
                            community.restore_from_main_table(new_life_strategies)
                            continue
                        else:
                            continue # cycle

                    # Store values for each community
                    leaf_litter[i] = daily_output.litter_l
                    root_litter[i] = daily_output.litter_fr
                    cwd[i] = daily_output.cwd
                    lnc[:, i] = daily_output.lnc
                    c_to_nfixers[i] = daily_output.cp[3]
                    evavg[i] = daily_output.evavg
                    epavg[i] = daily_output.epavg

                    if save:
                        nupt[:, i] = daily_output.nupt
                        pupt[:, i] = daily_output.pupt
                        leaf_litter[i] = daily_output.litter_l
                        c_to_nfixers[i] = daily_output.cp[3]
                        cwd[i] = daily_output.cwd
                        root_litter[i] = daily_output.litter_fr
                        lnc[:, i] = daily_output.lnc
                        cc[i] = daily_output.c_cost_cwm
                        npp[i] = daily_output.nppavg
                        photo[i] = daily_output.phavg
                        aresp[i] = daily_output.aravg
                        lai[i] = daily_output.laiavg
                        rcm[i] = daily_output.rcavg
                        f5[i] = daily_output.f5avg
                        rm[i] = daily_output.rmavg
                        rg[i] = daily_output.rgavg
                        cleaf[i] = daily_output.cp[0]
                        cawood[i] = daily_output.cp[1]
                        cfroot[i] = daily_output.cp[2]
                        wue[i] = daily_output.wueavg
                        cue[i] = daily_output.cueavg
                        cdef[i] = daily_output.c_defavg
                        vcmax[i] = daily_output.vcmax
                        specific_la[i] = daily_output.specific_la
                        storage_pool[:, i] = daily_output.stodbg.mean(axis=1)
                        ocp_area[:, i] = np.array(daily_output.ocpavg * 1e6, dtype='int32')
                        lim_status[:, :, i] = daily_output.limitation_status
                        uptake_strategy[:, :, i] = daily_output.uptk_strat
                    del daily_output
                #<- Out of the community loop
                del sto, cleaf_in, croot_in, cwood_in, uptk_costs # clean

                vpd = m.vapor_p_deficit(temp[step], ru[step])

                et_pot = epavg.mean()
                et = evavg.mean()

                self.evapm[step] = atm_canopy_coupling(et_pot, et, temp[step], vpd)
                self.runom[step] = self.swp._update_pool(prec[step], self.evapm[step])
                self.swp.w1 = 0.0 if self.swp.w1 < 0.0 else self.swp.w1
                self.swp.w2 = 0.0 if self.swp.w2 < 0.0 else self.swp.w2
                self.wp_water_upper_mm = self.swp.w1
                self.wp_water_lower_mm = self.swp.w2



                # CWM of STORAGE_POOL
                for i in range(3):
                    self.storage_pool[i, step] = np.sum(
                        community.vp_ocp * community.vp_sto[i])

                self.litter_l[step] = leaf_litter.mean() + c_to_nfixers.mean()
                self.cwd[step] = cwd.mean()
                self.litter_fr[step] = root_litter.mean()
                self.lnc[:, step] = lnc.mean(axis=1,)

                wtot = self.wp_water_upper_mm + self.wp_water_lower_mm
                s_out = soil_dec.carbon3(self.soil_temp, wtot / self.wmax_mm, self.litter_l[step],
                                         self.cwd[step], self.litter_fr[step], self.lnc[:, step],
                                         self.sp_csoil, self.sp_snc)
                soil_out = catch_out_carbon3(s_out)


                # Organic C N & P
                self.sp_csoil = soil_out['cs']
                self.sp_snc = soil_out['snc']
                idx = np.where(self.sp_snc < 0.0)[0]
                if len(idx) > 0:
                    self.sp_snc[idx] = 0.0

                # <- Out of the community loop

                # IF NUTRICYCLE:
                if nutri_cycle:
                    # UPDATE ORGANIC POOLS
                    self.sp_organic_n = self.sp_snc[:2].sum()
                    self.sp_sorganic_n = self.sp_snc[2:4].sum()
                    self.sp_organic_p = self.sp_snc[4:6].sum()
                    self.sp_sorganic_p = self.sp_snc[6:].sum()
                    self.sp_available_p += soil_out['pmin']
                    self.sp_available_n += soil_out['nmin']
                    # NUTRIENT DINAMICS
                    # Inorganic N
                    self.sp_in_n += self.sp_available_n + self.sp_so_n
                    self.sp_so_n = soil_dec.sorbed_n_equil(self.sp_in_n)
                    self.sp_available_n = soil_dec.solution_n_equil(
                        self.sp_in_n)
                    self.sp_in_n -= self.sp_so_n + self.sp_available_n
                    # Inorganic P
                    self.sp_in_p += self.sp_available_p + self.sp_so_p
                    self.sp_so_p = soil_dec.sorbed_p_equil(self.sp_in_p)
                    self.sp_available_p = soil_dec.solution_p_equil(
                        self.sp_in_p)
                    self.sp_in_p -= self.sp_so_p + self.sp_available_p
                    # Sorbed P
                    if self.pupt[1, step] > 0.75:
                        rwarn(
                            f"Puptk_SO > soP_max - 987 | in spin{s}, step{step} - {self.pupt[1, step]}")
                        self.pupt[1, step] = 0.0

                    if self.pupt[1, step] > self.sp_so_p:
                        rwarn(
                            f"Puptk_SO > soP_pool - 992 | in spin{s}, step{step} - {self.pupt[1, step]}")
                    self.sp_so_p -= self.pupt[1, step]
                    try:
                        t1 = np.all(self.sp_snc > 0.0)
                    except:
                        if self.sp_snc is None:
                            self.sp_snc = np.zeros(shape=8,)
                            t1 = True
                        elif self.sp_snc is not None:
                            t1 = True
                        rwarn(f"Exception while handling sp_snc pool")
                    if not t1:
                        self.sp_snc[np.where(self.sp_snc < 0)[0]] = 0.0
                    # ORGANIC nutrients uptake
                    # N
                    if self.nupt[1, step] < 0.0:
                        rwarn(
                            f"NuptkO < 0 - 1003 | in spin{s}, step{step} - {self.nupt[1, step]}")
                        self.nupt[1, step] = 0.0
                    if self.nupt[1, step] > 2.5:
                        rwarn(
                            f"NuptkO  > max - 1007 | in spin{s}, step{step} - {self.nupt[1, step]}")
                        self.nupt[1, step] = 0.0
                    total_on = self.sp_snc[:4].sum()
                    if total_on > 0.0:
                        frsn = [i / total_on for i in self.sp_snc[:4]]
                    else:
                        frsn = [0.0, 0.0, 0.0, 0.0]
                    for i, fr in enumerate(frsn):
                        self.sp_snc[i] -= self.nupt[1, step] * fr

                    idx = np.where(self.sp_snc < 0.0)[0]
                    if len(idx) > 0:
                        self.sp_snc[idx] = 0.0

                    self.sp_organic_n = self.sp_snc[:2].sum()
                    self.sp_sorganic_n = self.sp_snc[2:4].sum()

                    # P
                    if self.pupt[2, step] < 0.0:
                        rwarn(
                            f"PuptkO < 0  in spin{s}, step{step} - {self.pupt[2, step]}")
                        self.pupt[2, step] = 0.0
                    if self.pupt[2, step] > 1.0:
                        rwarn(
                            f"PuptkO > max  in spin{s}, step{step} - {self.pupt[2, step]}")
                        self.pupt[2, step] = 0.0
                    total_op = self.sp_snc[4:].sum()
                    if total_op > 0.0:
                        frsp = [i / total_op for i in self.sp_snc[4:]]
                    else:
                        frsp = [0.0, 0.0, 0.0, 0.0]
                    for i, fr in enumerate(frsp):
                        self.sp_snc[i + 4] -= self.pupt[2, step] * fr

                    idx = np.where(self.sp_snc < 0.0)[0]
                    if len(idx) > 0:
                        self.sp_snc[idx] = 0.0

                    self.sp_organic_p = self.sp_snc[4:6].sum()
                    self.sp_sorganic_p = self.sp_snc[6:].sum()

                    # Raise some warnings
                    if self.sp_organic_n < 0.0:
                        self.sp_organic_n = 0.0
                        rwarn(f"ON negative in spin{s}, step{step}")
                    if self.sp_sorganic_n < 0.0:
                        self.sp_sorganic_n = 0.0
                        rwarn(f"SON negative in spin{s}, step{step}")
                    if self.sp_organic_p < 0.0:
                        self.sp_organic_p = 0.0
                        rwarn(f"OP negative in spin{s}, step{step}")
                    if self.sp_sorganic_p < 0.0:
                        self.sp_sorganic_p = 0.0
                        rwarn(f"SOP negative in spin{s}, step{step}")

                    # CALCULATE THE EQUILIBTIUM IN SOIL POOLS
                    # Soluble and inorganic pools
                    if self.pupt[0, step] > 1e2:
                        rwarn(
                            f"Puptk > max - 786 | in spin{s}, step{step} - {self.pupt[0, step]}")
                        self.pupt[0, step] = 0.0
                    self.sp_available_p -= self.pupt[0, step]

                    if self.nupt[0, step] > 1e3:
                        rwarn(
                            f"Nuptk > max - 792 | in spin{s}, step{step} - {self.nupt[0, step]}")
                        self.nupt[0, step] = 0.0
                    self.sp_available_n -= self.nupt[0, step]
                # END SOIL NUTRIENT DYNAMICS

                if save:
                    # Plant uptake and Carbon costs of nutrient uptake
                    self.nupt[:, step] = nupt.mean(axis=1,)
                    self.pupt[:, step] = pupt.mean(axis=1,)
                    self.carbon_costs[step] = cc.mean()
                    self.tsoil.append(self.soil_temp)
                    self.photo[step] = photo.mean()
                    self.aresp[step] = aresp.mean()
                    self.npp[step] = npp.mean()
                    self.lai[step] = lai.mean()
                    self.rcm[step] = rcm.mean()
                    self.f5[step] = f5.mean()
                    self.rm[step] = rm.mean()
                    self.rg[step] = rg.mean()
                    self.wue[step] = wue.mean()
                    self.cue[step] = cue.mean()
                    self.cdef[step] = cdef.mean()
                    self.vcmax[step] = vcmax.mean()
                    self.specific_la[step] = specific_la.mean()
                    self.cleaf[step] = cleaf.mean()
                    self.cawood[step] = cawood.mean()
                    self.cfroot[step] = cfroot.mean()
                    self.hresp[step] = soil_out['hr']
                    self.csoil[:, step] = soil_out['cs']
                    self.wsoil[step] = self.wp_water_upper_mm + self.wp_water_lower_mm
                    self.inorg_n[step] = self.sp_in_n
                    self.inorg_p[step] = self.sp_in_p
                    self.sorbed_n[step] = self.sp_so_n
                    self.sorbed_p[step] = self.sp_so_p
                    self.snc[:, step] = soil_out['snc']
                    self.nmin[step] = self.sp_available_n
                    self.pmin[step] = self.sp_available_p
                    self.ocp_area[:,:, step] = ocp_area
                    self.lim_status[:, :, :, step] = lim_status
                    self.uptake_strategy[:, :, :, step] = uptake_strategy

                    del nupt, pupt, cc, tsoil, photo, aresp, npp, lai, rcm, f5
                    del wsoil, rm, rg, wue, cue, cdef, vcmax, specific_la, cleaf, cawood, cfroot
            # <- Out of the daily loop
            if save:
                if s > 0:
                    while True:
                        if sv.is_alive():
                            sleep(0.05)
                        else:
                            self.flush_data = None
                            break
                self.flush_data = self._flush_output(
                    'spin', (start_index, end_index))
                sv = Thread(target=self._save_output, args=(self.flush_data,))
                sv.start()
        # Finish the last thread
        # <- Out of spin loop
        if save:
            while True:
                if sv.is_alive():
                    sleep(0.05)
                else:
                    self.flush_data = None
                    break
        # Restablish new communities in the end, if applicable
        if kill_and_reset:
            for community in self.metacomm:
                with lock:
                    new_life_strategies = self.get_from_main_array(community.npls)
                community.restore_from_main_table(new_life_strategies)
        return None


    def get_spin(self, spin) -> dict:
        """Get the data from a given spin"""
        if len(self.outputs) == 0:
            raise AssertionError("No output data available. Run the model first")
        if spin < 10:
            name = f'spin0{spin}.pkz'
        else:
            name = f'spin{spin}.pkz'
        with open(self.outputs[name], 'rb') as fh:
            spin_dt = load(fh)
        return spin_dt


    def __getattribute__(self, name:str) -> np.ndarray:
        return super().__getattribute__(name)


    def __getitem__(self, name:str):
        return self.__getattribute__(name)


class region:
    """Region class containing the gridcells for a given region
    """

    def __init__(self,
                name:str,
                clim_data:Union[str,Path],
                soil_data:Tuple[Tuple[np.ndarray], Tuple[np.ndarray], Tuple[np.ndarray]],
                co2:Union[str, Path],
                pls_table:np.ndarray)->None:
        """_summary_

        Args:
            name (str): this will be the name of the region and the name of the output folder
            clim_data (Union[str,Path]): Path for the climate data
            soil_data (Tuple[Tuple[np.ndarray], Tuple[np.ndarray], Tuple[np.ndarray]]): _description_
            output_folder (Union[str, Path]): _description_
            co2 (Union[str, Path]): _description_
            pls_table (np.ndarray): _description_
        """
        self.config = fetch_config("caete.toml")
        self.nproc = self.config.multiprocessing.nprocs
        self.name = Path(name)
        self.co2_path = str_or_path(co2)
        self.co2_data = get_co2_concentration(self.co2_path)

        # IO
        self.climate_files = []
        self.input_data = str_or_path(clim_data)
        self.soil_data = copy.deepcopy(soil_data)
        self.pls_table = mc.pls_table(pls_table)
        self.grid = np.ones((360, 720), dtype=bool)
        self.npls_main_table = self.pls_table.npls

        try:
            metadata_file = list(self.input_data.glob("*_METADATA.pbz2"))[0]
        except:
            raise FileNotFoundError("Metadata file not found in the input data folder")

        try:
            mtd = str_or_path(metadata_file, check_is_file=True)
        except:
            raise AssertionError("Metadata file path could not be resolved. Cannot proceed without metadata")

        self.metadata = read_bz2_file(mtd)
        self.stime = copy.deepcopy(self.metadata[0])

        for file_path in self.input_data.glob("input_data_*-*.pbz2"):
            self.climate_files.append(file_path)

        self.yx_indices = []
        for f in self.climate_files:
            y, x = f.stem.split("_")[-1].split("-")
            self.yx_indices.append((int(y), int(x)))
            self.grid[int(y), int(x)] = False

        # create the output folder structure
        # This is the output path for the regions, Create it if it does not exist
        os.makedirs(output_path, exist_ok=True)

        # This is the output path for this region
        self.output_path = output_path/self.name
        os.makedirs(self.output_path, exist_ok=True)

        # A list to store this region's gridcells
        self.gridcells:List[grd_mt] = []


    def get_from_main_table(self, comm_npls):

        """Returns a number of IDs (in the main table) and the respective
        functional identities (PLS table) to set or reset a community

        Args:
        comm_npls: (int) Number of PLS in the output table (must match npls_max (see caete.toml))"""
        idx = np.random.randint(0, self.npls_main_table - 1, comm_npls)
        return idx, self.pls_table.table[:, idx]


    def set_gridcells(self):
        print("Starting gridcells")
        i = 0
        print_progress(i, len(self.yx_indices), prefix='Progress:', suffix='Complete')
        for f,pos in zip(self.climate_files, self.yx_indices):
            y, x = pos
            file_name = self.output_path/Path(f"grd_{y}-{x}")
            grd_cell = grd_mt(y, x, file_name, self.get_from_main_table)
            grd_cell.set_gridcell(f, stime_i=self.stime, co2=self.co2_data,
                                    tsoil=tsoil, ssoil=ssoil, hsoil=hsoil)
            self.gridcells.append(grd_cell)
            print_progress(i+1, len(self.yx_indices), prefix='Progress:', suffix='Complete')
            i += 1


    def run_region_map(self, func:Callable):
        with mp.Pool(processes=self.nproc, maxtasksperchild=1) as p:
            self.gridcells = p.map(func, self.gridcells, chunksize=1)
        gc.collect()
        return None


    def run_region_starmap(self, func:Callable, args):
        with mp.Pool(processes=self.nproc, maxtasksperchild=1) as p:
            self.gridcells = p.starmap(func, [(gc, args) for gc in self.gridcells], chunksize=1)
        gc.collect()
        return None


    def get_mask(self)->np.ndarray:
        return self.grid


    def __getitem__(self, idx:int):
        try:
            _val_ = self.gridcells[idx]
        except IndexError:
            raise IndexError(f"Cannot get item at index {idx}. Region has {self.__len__()} gridcells")
        return _val_


    def __len__(self):
        return len(self.gridcells)


    def __iter__(self):
        yield from self.gridcells


class worker:
    """Worker functions used to run the model in parallel"""


    def __init__(self):
        return None


    @staticmethod
    def create_run_breaks(start_year:int, end_year:int, interval:int):
        run_breaks_hist = []
        current_year = start_year

        # Create intervals
        while current_year + interval - 1 <= end_year:
            start_date = f"{current_year}0101"
            end_date = f"{current_year + interval - 1}1231"
            run_breaks_hist.append((start_date, end_date))
            current_year += interval

        # Adjust the last interval if it is not uniform
        if current_year <= end_year:
            start_date = f"{current_year}0101"
            end_date = f"{end_year}1231"
            run_breaks_hist.append((start_date, end_date))

        return run_breaks_hist


    @staticmethod
    def soil_pools_spinup(gridcell:grd_mt):
        """spin to attain equilibrium in soil pools"""
        gridcell.run_gridcell("1901-01-01", "1930-12-31", spinup=10, fixed_co2_atm_conc="1901",
                              save=False, nutri_cycle=False, reset_community=True, kill_and_reset=True)
        gc.collect()
        return gridcell


    @staticmethod
    def community_spinup(gridcell:grd_mt):
        """spin to attain equilibrium in the community"""
        gridcell.run_gridcell("1901-01-01", "1930-12-31", spinup=10, fixed_co2_atm_conc="1901",
                              save=False, nutri_cycle=True, reset_community=True, kill_and_reset=False)
        gc.collect()
        return gridcell


    @staticmethod
    def transient_run(gridcell:grd_mt):
        """transient run"""
        gridcell.run_gridcell("1901-01-01", "2016-12-31", spinup=0, fixed_co2_atm_conc=None,
                              save=True, nutri_cycle=True, reset_community=False, kill_and_reset=False)
        gc.collect()
        return gridcell


    @staticmethod
    def transient_run_brk(gridcell:grd_mt, interval:Tuple[str, str]):
        """transient run"""
        start_date, end_date = interval
        gridcell.run_gridcell(start_date, end_date, spinup=0, fixed_co2_atm_conc=None,
                              save=True, nutri_cycle=True, reset_community=False, kill_and_reset=False)
        gc.collect()
        return gridcell


    @staticmethod
    def save_state(region:region, fname:Union[str, Path]):
        with bz2.BZ2File(fname, mode='wb') as fh:
            pkl.dump(region, fh)


# -----------------------------------
# OLD CAETÊ
# This is the prototype implementation of CAETÊ that I created during my PhD.
# Will continue here for some time

# GLOBAL variables
out_ext = ".pkz"
npls = gp.npls

NO_DATA = [-9999.0, -9999.0]


run_breaks_hist = [('19790101', '19801231'),
                   ('19810101', '19821231'),
                   ('19830101', '19841231'),
                   ('19850101', '19861231'),
                   ('19870101', '19881231'),
                   ('19890101', '19901231'),
                   ('19910101', '19921231'),
                   ('19930101', '19941231'),
                   ('19950101', '19961231'),
                   ('19970101', '19981231'),
                   ('19990101', '20001231'),
                   ('20010101', '20021231'),
                   ('20030101', '20041231'),
                   ('20050101', '20061231'),
                   ('20070101', '20081231'),
                   ('20090101', '20101231'),
                   ('20110101', '20121231'),
                   ('20130101', '20141231'),
                   ('20150101', '20161231')]

run_breaks_CMIP5_hist = [('19300101', '19391231'),
                        ('19400101', '19491231'),
                        ('19500101', '19591231'),
                        ('19600101', '19691231'),
                        ('19700101', '19791231'),
                        ('19800101', '19891231'),
                        ('19900101', '19991231'),
                        ('20000101', '20051231')]

run_breaks_CMIP5_proj = [('20060101', '20091231'),
                         ('20100101', '20191231'),
                         ('20200101', '20291231'),
                         ('20300101', '20391231'),
                         ('20400101', '20491231'),
                         ('20500101', '20591231'),
                         ('20600101', '20691231'),
                         ('20700101', '20791231'),
                         ('20800101', '20891231'),
                         ('20900101', '20991231')]

# historical and projection periods respectively
rbrk = [run_breaks_hist, run_breaks_CMIP5_hist, run_breaks_CMIP5_proj]


class grd:

    """
    Defines the gridcell object - This object stores all the input data,
    the data comming from model runs for each grid point, all the state variables and all the metadata
    describing the life cycle of the gridcell and the filepaths to the generated model outputs
    This class also provides several methods to apply the CAETÊ model with proper formated climatic and soil variables
    """

    def __init__(self, x, y, dump_folder):
        """Construct the gridcell object"""

        # CELL Identifiers
        self.x = x                            # Grid point x coordinate
        self.y = y                            # Grid point y coordinate
        self.xyname = str(y) + '-' + str(x)   # IDENTIFIES GRIDCELLS
        self.plot_name = dump_folder
        self.plot = None
        self.input_fname = f"input_data_{self.xyname}.pbz2"
        self.input_fpath = None
        self.data = None
        self.pos = (int(self.x), int(self.y))
        self.pls_table = None   # will receive the np.array with functional traits data
        self.outputs = {}       # dict, store filepaths of output data
        self.realized_runs = []
        self.experiments = 1
        # counts the execution of a time slice (a call of self.run_spinup)
        self.run_counter = 0
        self.neighbours = None

        self.ls = None          # Number of surviving plss//
        self.grid_filename = f"gridcell{self.xyname}"
        self.out_dir = Path(
            "../outputs/{}/gridcell{}/".format(dump_folder, self.xyname)).resolve()
        self.flush_data = None

        # Time attributes
        self.time_index = None  # Array with the time stamps
        self.calendar = None    # Calendar name
        self.time_unit = None   # Time unit
        self.start_date = None
        self.end_date = None
        self.ssize = None
        self.sind = None
        self.eind = None

        # Input data
        self.filled = False     # Indicates when the gridcell is filled with input data
        self.pr = None
        self.ps = None
        self.rsds = None
        self.tas = None
        self.rhs = None

        # OUTPUTS
        self.soil_temp = None
        self.emaxm = None
        self.tsoil = None
        self.photo = None
        self.ls  = None
        self.aresp = None
        self.npp = None
        self.lai = None
        self.csoil = None
        self.inorg_n = None
        self.inorg_p = None
        self.sorbed_n = None
        self.sorbed_p = None
        self.snc = None
        self.hresp = None
        self.rcm = None
        self.f5 = None
        self.runom = None
        self.evapm = None
        self.wsoil = None
        self.swsoil = None
        self.rm = None
        self.rg = None
        self.cleaf = None
        self.cawood = None
        self.cfroot = None
        self.area = None
        self.wue = None
        self.cue = None
        self.cdef = None
        self.nmin = None
        self.pmin = None
        self.vcmax = None
        self.specific_la = None
        self.nupt = None
        self.pupt = None
        self.litter_l = None
        self.cwd = None
        self.litter_fr = None
        self.lnc = None
        self.storage_pool = None
        self.lim_status = None
        self.uptake_strategy = None
        self.carbon_costs = None

        # WATER POOLS
        # Water content for each soil layer
        self.wp_water_upper_mm = None  # mm
        self.wp_water_lower_mm = None  # mm
        # Saturation point
        self.wmax_mm = None  # mm

        # SOIL POOLS
        self.input_nut = None
        self.sp_available_p = None
        self.sp_available_n = None
        self.sp_so_n = None
        self.sp_in_n = None
        self.sp_so_p = None
        self.sp_in_p = None
        self.sp_csoil = None
        self.sp_snr = None
        self.sp_uptk_costs = None
        self.sp_organic_n = None
        self.sp_sorganic_n = None
        self.sp_organic_p = None
        self.sp_sorganic_p = None

        # CVEG POOLS
        self.vp_cleaf = None
        self.vp_croot = None
        self.vp_cwood = None
        self.vp_dcl = None
        self.vp_dca = None
        self.vp_dcf = None
        self.vp_ocp = None
        self.vp_wdl = None
        self.vp_sto = None
        self.vp_lsid = None

        # Hydraulics
        self.theta_sat = None
        self.psi_sat = None
        self.soil_texture = None

    def _allocate_output_nosave(self, n):
        """allocate space for some tracked variables during spinup
        n: int NUmber of days being simulated"""

        self.runom = np.zeros(shape=(n,), order='F')
        self.nupt = np.zeros(shape=(2, n), order='F')
        self.pupt = np.zeros(shape=(3, n), order='F')
        self.litter_l = np.zeros(shape=(n,), order='F')
        self.cwd = np.zeros(shape=(n,), order='F')
        self.litter_fr = np.zeros(shape=(n,), order='F')
        self.lnc = np.zeros(shape=(6, n), order='F')
        self.storage_pool = np.zeros(shape=(3, n), order='F')
        self.ls = np.zeros(shape=(n,), order='F')

    def _allocate_output(self, n, npls=npls):
        """allocate space for the outputs
        n: int NUmber of days being simulated"""
        self.emaxm = []
        self.tsoil = []
        self.photo = np.zeros(shape=(n,), order='F')
        self.aresp = np.zeros(shape=(n,), order='F')
        self.npp = np.zeros(shape=(n,), order='F')
        self.lai = np.zeros(shape=(n,), order='F')
        self.csoil = np.zeros(shape=(4, n), order='F')
        self.inorg_n = np.zeros(shape=(n,), order='F')
        self.inorg_p = np.zeros(shape=(n,), order='F')
        self.sorbed_n = np.zeros(shape=(n,), order='F')
        self.sorbed_p = np.zeros(shape=(n,), order='F')
        self.snc = np.zeros(shape=(8, n), order='F')
        self.hresp = np.zeros(shape=(n,), order='F')
        self.rcm = np.zeros(shape=(n,), order='F')
        self.f5 = np.zeros(shape=(n,), order='F')
        self.runom = np.zeros(shape=(n,), order='F')
        self.evapm = np.zeros(shape=(n,), order='F')
        self.wsoil = np.zeros(shape=(n,), order='F')
        self.swsoil = np.zeros(shape=(n,), order='F')
        self.rm = np.zeros(shape=(n,), order='F')
        self.rg = np.zeros(shape=(n,), order='F')
        self.cleaf = np.zeros(shape=(n,), order='F')
        self.cawood = np.zeros(shape=(n,), order='F')
        self.cfroot = np.zeros(shape=(n,), order='F')
        self.wue = np.zeros(shape=(n,), order='F')
        self.cue = np.zeros(shape=(n,), order='F')
        self.cdef = np.zeros(shape=(n,), order='F')
        self.nmin = np.zeros(shape=(n,), order='F')
        self.pmin = np.zeros(shape=(n,), order='F')
        self.vcmax = np.zeros(shape=(n,), order='F')
        self.specific_la = np.zeros(shape=(n,), order='F')
        self.nupt = np.zeros(shape=(2, n), order='F')
        self.pupt = np.zeros(shape=(3, n), order='F')
        self.litter_l = np.zeros(shape=(n,), order='F')
        self.cwd = np.zeros(shape=(n,), order='F')
        self.litter_fr = np.zeros(shape=(n,), order='F')
        self.lnc = np.zeros(shape=(6, n), order='F')
        self.storage_pool = np.zeros(shape=(3, n), order='F')
        self.ls = np.zeros(shape=(n,), order='F')
        self.carbon_costs = np.zeros(shape=(n,), order='F')

        self.area = np.zeros(shape=(npls, n), order='F')
        self.lim_status = np.zeros(
            shape=(3, npls, n), dtype=np.dtype('int16'), order='F')
        self.uptake_strategy = np.zeros(
            shape=(2, npls, n), dtype=np.dtype('int32'), order='F')

    def _flush_output(self, run_descr, index):
        """1 - Clean variables that receive outputs from the fortran subroutines
           2 - Fill self.outputs dict with filepats of output data
           3 - Returns the output data to be writen

           runs_descr: str a name for the files
           index = tuple or list with the first and last values of the index time variable"""
        to_pickle = {}
        self.run_counter += 1
        if self.run_counter < 10:
            spiname = run_descr + "0" + str(self.run_counter) + out_ext
        else:
            spiname = run_descr + str(self.run_counter) + out_ext

        self.outputs[spiname] = os.path.join(self.out_dir, spiname)
        to_pickle = {'emaxm': np.array(self.emaxm),
                     "tsoil": np.array(self.tsoil),
                     "photo": self.photo,
                     "aresp": self.aresp,
                     'npp': self.npp,
                     'lai': self.lai,
                     'csoil': self.csoil,
                     'inorg_n': self.inorg_n,
                     'inorg_p': self.inorg_p,
                     'sorbed_n': self.sorbed_n,
                     'sorbed_p': self.sorbed_p,
                     'snc': self.snc,
                     'hresp': self.hresp,
                     'rcm': self.rcm,
                     'f5': self.f5,
                     'runom': self.runom,
                     'evapm': self.evapm,
                     'wsoil': self.wsoil,
                     'swsoil': self.swsoil,
                     'rm': self.rm,
                     'rg': self.rg,
                     'cleaf': self.cleaf,
                     'cawood': self.cawood,
                     'cfroot': self.cfroot,
                     'area': self.area,
                     'wue': self.wue,
                     'cue': self.cue,
                     'cdef': self.cdef,
                     'nmin': self.nmin,
                     'pmin': self.pmin,
                     'vcmax': self.vcmax,
                     'specific_la': self.specific_la,
                     'nupt': self.nupt,
                     'pupt': self.pupt,
                     'litter_l': self.litter_l,
                     'cwd': self.cwd,
                     'litter_fr': self.litter_fr,
                     'lnc': self.lnc,
                     'ls': self.ls,
                     'lim_status': self.lim_status,
                     'c_cost': self.carbon_costs,
                     'u_strat': self.uptake_strategy,
                     'storage_pool': self.storage_pool,
                     'calendar': self.calendar,    # Calendar name
                     'time_unit': self.time_unit,   # Time unit
                     'sind': index[0],
                     'eind': index[1]}
        # Flush attrs
        self.emaxm = []
        self.tsoil = []
        self.photo = None
        self.aresp = None
        self.npp = None
        self.lai = None
        self.csoil = None
        self.inorg_n = None
        self.inorg_p = None
        self.sorbed_n = None
        self.sorbed_p = None
        self.snc = None
        self.hresp = None
        self.rcm = None
        self.f5 = None
        self.runom = None
        self.evapm = None
        self.wsoil = None
        self.swsoil = None
        self.rm = None
        self.rg = None
        self.cleaf = None
        self.cawood = None
        self.cfroot = None
        self.area = None
        self.wue = None
        self.cue = None
        self.cdef = None
        self.nmin = None
        self.pmin = None
        self.vcmax = None
        self.specific_la = None
        self.nupt = None
        self.pupt = None
        self.litter_l = None
        self.cwd = None
        self.litter_fr = None
        self.lnc = None
        self.storage_pool = None
        self.ls = None
        self.ls_id = None
        self.lim_status = None
        self.carbon_costs = None,
        self.uptake_strategy = None

        return to_pickle

    def _save_output(self, data_obj):
        """Compress and save output data
        data_object: dict; the dict returned from _flush_output"""
        if self.run_counter < 10:
            fpath = "spin{}{}{}".format(0, self.run_counter, out_ext)
        else:
            fpath = "spin{}{}".format(self.run_counter, out_ext)
        with open(self.outputs[fpath], 'wb') as fh:
            dump(data_obj, fh, compress=('lz4', 9), protocol=4)
        self.flush_data = 0

    def init_caete_dyn(self, input_fpath, stime_i, co2, pls_table, tsoil, ssoil, hsoil):
        """ PREPARE A GRIDCELL TO RUN
            input_fpath:(str or pathlib.Path) path to Files with climate and soil data
            co2: (list) a alist (association list) with yearly cCO2 ATM data(yyyy\t[CO2]atm\n)
            pls_table: np.ndarray with functional traits of a set of PLant life strategies
        """

        assert self.filled == False, "already done"
        self.input_fpath = Path(os.path.join(input_fpath, self.input_fname))
        assert self.input_fpath.exists()

        with bz2.BZ2File(self.input_fpath, mode='r') as fh:
            self.data = pkl.load(fh)

        os.makedirs(self.out_dir, exist_ok=True)
        self.flush_data = 0

        # # Metacomunity
        # self.metacomm = metacommunity(pls_table=self.pls_table)

        self.pr = self.data['pr']
        self.ps = self.data['ps']
        self.rsds = self.data['rsds']
        self.tas = self.data['tas']
        self.rhs = self.data['hurs']

        # SOIL AND NUTRIENTS
        self.input_nut = []
        self.nutlist = ['tn', 'tp', 'ap', 'ip', 'op']
        for nut in self.nutlist:
            self.input_nut.append(self.data[nut])
        self.soil_dict = dict(zip(self.nutlist, self.input_nut))
        self.data = None

        # TIME
        self.stime = copy.deepcopy(stime_i)
        self.calendar = self.stime['calendar']
        self.time_index = self.stime['time_index']
        self.time_unit = self.stime['units']
        self.ssize = self.time_index.size
        self.sind = int(self.time_index[0])
        self.eind = int(self.time_index[-1])
        self.start_date = cftime.num2date(
            self.time_index[0], self.time_unit, calendar=self.calendar)
        self.end_date = cftime.num2date(
            self.time_index[-1], self.time_unit, calendar=self.calendar)

        # OTHER INPUTS
        self.pls_table = copy.deepcopy(pls_table)
        # self.neighbours = neighbours_index(self.pos, mask)
        self.soil_temp = st.soil_temp_sub(self.tas[:1095] - 273.15)

        # Prepare co2 inputs (we have annually means)
        self.co2_data = copy.deepcopy(co2)

        self.tsoil = []
        self.emaxm = []

        # STATE
        # Water
        self.ws1 = tsoil[0][self.y, self.x].copy()
        self.fc1 = tsoil[1][self.y, self.x].copy()
        self.wp1 = tsoil[2][self.y, self.x].copy()
        self.ws2 = ssoil[0][self.y, self.x].copy()
        self.fc2 = ssoil[1][self.y, self.x].copy()
        self.wp2 = ssoil[2][self.y, self.x].copy()

        self.swp = soil_water(self.ws1, self.ws2, self.fc1, self.fc2, self.wp1, self.wp2)
        self.wp_water_upper_mm = self.swp.w1
        self.wp_water_lower_mm = self.swp.w2
        self.wmax_mm = np.float64(self.swp.w1_max + self.swp.w2_max)

        self.theta_sat = hsoil[0][self.y, self.x].copy()
        self.psi_sat = hsoil[1][self.y, self.x].copy()
        self.soil_texture = hsoil[2][self.y, self.x].copy()

        # Biomass
        self.vp_cleaf = np.random.uniform(0.3,0.4,npls)#np.zeros(shape=(npls,), order='F') + 0.1
        self.vp_croot = np.random.uniform(0.3,0.4,npls)#np.zeros(shape=(npls,), order='F') + 0.1
        self.vp_cwood = np.random.uniform(5.0,6.0,npls)#np.zeros(shape=(npls,), order='F') + 0.1

        self.vp_cwood[pls_table[6,:] == 0.0] = 0.0

        a, b, c, d = m.pft_area_frac(
            self.vp_cleaf, self.vp_croot, self.vp_cwood, self.pls_table[6, :])
        del b # not used
        del c # not used
        del d # not used
        self.vp_lsid = np.where(a > 0.0)[0]
        self.ls = self.vp_lsid.size
        self.vp_dcl = np.zeros(shape=(npls,), order='F')
        self.vp_dca = np.zeros(shape=(npls,), order='F')
        self.vp_dcf = np.zeros(shape=(npls,), order='F')
        self.vp_ocp = np.zeros(shape=(npls,), order='F')
        self.vp_sto = np.zeros(shape=(3, npls), order='F')

        # # # SOIL
        self.sp_csoil = np.zeros(shape=(4,), order='F') + 0.001
        self.sp_snc = np.zeros(shape=(8,), order='F') + 0.0001
        self.sp_available_p = self.soil_dict['ap']
        self.sp_available_n = 0.2 * self.soil_dict['tn']
        self.sp_in_n = 0.4 * self.soil_dict['tn']
        self.sp_so_n = 0.2 * self.soil_dict['tn']
        self.sp_so_p = self.soil_dict['tp'] - sum(self.input_nut[2:])
        self.sp_in_p = self.soil_dict['ip']
        self.sp_uptk_costs = np.zeros(npls, order='F')
        self.sp_organic_n = 0.1 * self.soil_dict['tn']
        self.sp_sorganic_n = 0.1 * self.soil_dict['tn']
        self.sp_organic_p = 0.5 * self.soil_dict['op']
        self.sp_sorganic_p = self.soil_dict['op'] - self.sp_organic_p

        self.outputs = dict()
        self.filled = True
        return None

    def clean_run(self, dump_folder, save_id):
        abort = False
        mem = str(self.out_dir)
        self.out_dir = Path(
            "../outputs/{}/gridcell{}/".format(dump_folder, self.xyname)).resolve()
        try:
            os.makedirs(str(self.out_dir), exist_ok=False)
        except FileExistsError:
            abort = True
            print(
                f"Folder {dump_folder} already exists. You cannot orerwrite its contents")
        finally:
            assert self.out_dir.exists(), f"Failed to create {self.out_dir}"

        if abort:
            print("ABORTING")
            self.out_dir = Path(mem)
            print(
                f"Returning the original grd_{self.xyname}.out_dir to {self.out_dir}")
            raise RuntimeError

        self.realized_runs.append((save_id, self.outputs.copy()))
        self.outputs = {}
        self.run_counter = 0
        self.experiments += 1

    def change_clim_input(self, input_fpath, stime_i, co2):

        self.input_fpath = Path(os.path.join(input_fpath, self.input_fname))
        assert self.input_fpath.exists()

        with bz2.BZ2File(self.input_fpath, mode='r') as fh:
            self.data = pkl.load(fh)

        self.flush_data = 0

        self.pr = self.data['pr']
        self.ps = self.data['ps']
        self.rsds = self.data['rsds']
        self.tas = self.data['tas']
        self.rhs = self.data['hurs']

        # SOIL AND NUTRIENTS
        self.input_nut = []
        self.nutlist = ['tn', 'tp', 'ap', 'ip', 'op']
        for nut in self.nutlist:
            self.input_nut.append(self.data[nut])
        self.soil_dict = dict(zip(self.nutlist, self.input_nut))
        self.data = None

        # TIME
        self.stime = copy.deepcopy(stime_i)
        self.calendar = self.stime['calendar']
        self.time_index = self.stime['time_index']
        self.time_unit = self.stime['units']
        self.ssize = self.time_index.size
        self.sind = int(self.time_index[0])
        self.eind = int(self.time_index[-1])
        self.start_date = cftime.num2date(
            self.time_index[0], self.time_unit, calendar=self.calendar)
        self.end_date = cftime.num2date(
            self.time_index[-1], self.time_unit, calendar=self.calendar)

        # Prepare co2 inputs (we have annually means)
        self.co2_data = copy.deepcopy(co2)

        return None

    def run_caete(self,
                  start_date,
                  end_date,
                  spinup=0,
                  fix_co2=None,
                  save=True,
                  nutri_cycle=True,
                  afex=False):
        """ start_date [str]   "yyyymmdd" Start model execution

            end_date   [str]   "yyyymmdd" End model execution

            spinup     [int]   Number of repetitions in spinup. 0 for no spinup

            fix_co2    [Float] Fixed value for ATM [CO2]
                       [int]   Fixed value for ATM [CO2]
                       [str]   "yyyy" Corresponding year of an ATM [CO2]

            This function run the fortran subroutines and manage data flux. It
            is the proper CAETÊ-DVM execution in the start_date - end_date period
        """

        assert self.filled, "The gridcell has no input data"
        assert not fix_co2 or type(
            fix_co2) == str or fix_co2 > 0, "A fixed value for ATM[CO2] must be a positive number greater than zero or a proper string "
        ABORT = 0
        if self.plot is True:
            splitter = ","
        else:
            splitter = "\t"

        def find_co2(year):
            for i in self.co2_data:
                if int(i.split(splitter)[0]) == year:
                    return float(i.split(splitter)[1].strip())

        def find_index(start, end):
            result = []
            num = np.arange(self.ssize)
            ind = np.arange(self.sind, self.eind + 1)
            for r, i in zip(num, ind):
                if i == start:
                    result.append(r)
            for r, i in zip(num, ind):
                if i == end:
                    result.append(r)
            return result

        # Define start and end dates (read actual arguments)
        start = cftime.real_datetime(int(start_date[:4]), int(
            start_date[4:6]), int(start_date[6:]))
        end = cftime.real_datetime(int(end_date[:4]), int(
            end_date[4:6]), int(end_date[6:]))
        # Check dates sanity
        assert start < end, "start > end"
        assert start >= self.start_date
        assert end <= self.end_date

        # Define time index
        start_index = int(cftime.date2num(
            start, self.time_unit, self.calendar))
        end_index = int(cftime.date2num(end, self.time_unit, self.calendar))

        lb, hb = find_index(start_index, end_index)
        steps = np.arange(lb, hb + 1)
        day_indexes = np.arange(start_index, end_index + 1)
        spin = 1 if spinup == 0 else spinup

        # Catch climatic input and make conversions
        temp = self.tas[lb: hb + 1] - 273.15  # ! K to °C
        prec = self.pr[lb: hb + 1] * 86400  # kg m-2 s-1 to  mm/day
        # transforamando de Pascal pra mbar (hPa)
        p_atm = self.ps[lb: hb + 1] * 0.01
        # W m-2 to mol m-2 s-1 ! 0.5 converts RSDS to PAR
        ipar = self.rsds[lb: hb + 1] * 0.5 / 2.18e5
        ru = self.rhs[lb: hb + 1] / 100.0

        year0 = start.year
        co2 = find_co2(year0)
        count_days = start.dayofyr - 2
        loop = 0
        next_year = 0.0

        fix_co2_p = False
        if fix_co2 is None:
            fix_co2_p = False
        elif type(fix_co2) == int or type(fix_co2) == float:
            co2 = fix_co2
            fix_co2_p = True
        elif type(fix_co2) == str:
            assert type(int(
                fix_co2)) == int, "The string(\"yyyy\") for the fix_co2 argument must be an year between 1901-2016"
            co2 = find_co2(int(fix_co2))
            fix_co2_p = True

        for s in range(spin):
            if ABORT:
                pID = os.getpid()
                print(f'Closed process PID = {pID}\nGRD = {self.plot_name}\nCOORD = {self.pos}')
                break
            if save:
                self._allocate_output(steps.size)
                self.save = True
            else:
                self._allocate_output_nosave(steps.size)
                self.save = False
            for step in range(steps.size):
                if fix_co2_p:
                    pass
                else:
                    loop += 1
                    count_days += 1
                    # CAST CO2 ATM CONCENTRATION
                    days = 366 if m.leap(year0) == 1 else 365
                    if count_days == days:
                        count_days = 0
                        year0 = cftime.num2date(day_indexes[step],
                                                self.time_unit, self.calendar).year
                        co2 = find_co2(year0)
                        next_year = (find_co2(year0 + 1) - co2) / days

                    elif loop == 1 and count_days < days:
                        year0 = start.year
                        next_year = (find_co2(year0 + 1) - co2) / \
                            (days - count_days)

                    co2 += next_year

                # Update soil temperature
                self.soil_temp = st.soil_temp(self.soil_temp, temp[step])

                # AFEX
                if count_days == 364 and afex:
                    with open("afex.cfg", 'r') as afex_cfg:
                        afex_exp = afex_cfg.readlines()
                    afex_exp = afex_exp[0].strip()
                    if afex_exp == 'N':
                        # (12.5 g m-2 y-1 == 125 kg ha-1 y-1)
                        self.sp_available_n += 12.5
                    elif afex_exp == 'P':
                        # (5 g m-2 y-1 == 50 kg ha-1 y-1)
                        self.sp_available_p += 5.0
                    elif afex_exp == 'NP':
                        self.sp_available_n += 12.5
                        self.sp_available_p += 5.0

                # INFLATe VARS
                sto = np.zeros(shape=(3, npls), order='F')
                cleaf = np.zeros(npls, order='F')
                cwood = np.zeros(npls, order='F')
                croot = np.zeros(npls, order='F')
                # dcl = np.zeros(npls, order='F')
                # dca = np.zeros(npls, order='F')
                # dcf = np.zeros(npls, order='F')
                uptk_costs = np.zeros(npls, order='F')

                sto[0, self.vp_lsid] = self.vp_sto[0, :]
                sto[1, self.vp_lsid] = self.vp_sto[1, :]
                sto[2, self.vp_lsid] = self.vp_sto[2, :]
                # Just Check the integrity of the data
                assert self.vp_lsid.size == self.vp_cleaf.size, 'different array sizes'
                c = 0
                for n in self.vp_lsid:
                    cleaf[n] = self.vp_cleaf[c]
                    cwood[n] = self.vp_cwood[c]
                    croot[n] = self.vp_croot[c]
                    # dcl[n] = self.vp_dcl[c]
                    # dca[n] = self.vp_dca[c]
                    # dcf[n] = self.vp_dcf[c]
                    uptk_costs[n] = self.sp_uptk_costs[c]
                    c += 1
                ton = self.sp_organic_n #+ self.sp_sorganic_n
                top = self.sp_organic_p #+ self.sp_sorganic_p
                # TODO need to adapt no assimilation with water content lower than thw wilting point
                # self.swp.w1_max ...
                out = model.daily_budget(self.pls_table, self.wp_water_upper_mm, self.wp_water_lower_mm,
                                         self.soil_temp, temp[step], p_atm[step],
                                         ipar[step], ru[step], self.sp_available_n, self.sp_available_p,
                                         ton, top, self.sp_organic_p, co2, sto, cleaf, cwood, croot,
                                         uptk_costs, self.wmax_mm)

                # del sto, cleaf, cwood, croot, dcl, dca, dcf, uptk_costs
                # Create a dict with the function output
                daily_output = catch_out_budget(out)

                self.vp_lsid = np.where(daily_output['ocpavg'] > 0.0)[0]
                self.vp_ocp = daily_output['ocpavg'][self.vp_lsid]
                self.ls[step] = self.vp_lsid.size

                if self.vp_lsid.size < 1 and not save:
                    self.vp_lsid = np.sort(
                        np.array(
                            rd.sample(list(np.arange(gp.npls)), int(gp.npls - 5))))
                    rwarn(
                        f"Gridcell {self.xyname} has no living Plant Life Strategies - Re-populating")
                    # REPOPULATE]
                    # UPDATE vegetation pools
                    self.vp_cleaf = np.zeros(shape=(self.vp_lsid.size,)) + 0.01
                    self.vp_cwood = np.zeros(shape=(self.vp_lsid.size,))
                    self.vp_croot = np.zeros(shape=(self.vp_lsid.size,)) + 0.01
                    awood = self.pls_table[6, :]
                    for i0, i in enumerate(self.vp_lsid):
                        if awood[i] > 0.0:
                            self.vp_cwood[i0] = 0.01

                    self.vp_dcl = np.zeros(shape=(self.vp_lsid.size,))
                    self.vp_dca = np.zeros(shape=(self.vp_lsid.size,))
                    self.vp_dcf = np.zeros(shape=(self.vp_lsid.size,))
                    self.vp_sto = np.zeros(shape=(3, self.vp_lsid.size))
                    self.sp_uptk_costs = np.zeros(shape=(self.vp_lsid.size,))

                    self.vp_ocp = np.zeros(shape=(self.vp_lsid.size,))
                    del awood
                    self.ls[step] = self.vp_lsid.size
                else:
                    if self.vp_lsid.size < 1:
                        ABORT = 1
                        rwarn(f"Gridcell {self.xyname} has"  + \
                               " no living Plant Life Strategies")
                    # UPDATE vegetation pools
                    self.vp_cleaf = daily_output['cleafavg_pft'][self.vp_lsid]
                    self.vp_cwood = daily_output['cawoodavg_pft'][self.vp_lsid]
                    self.vp_croot = daily_output['cfrootavg_pft'][self.vp_lsid]
                    # self.vp_dcl = daily_output['delta_cveg'][0][self.vp_lsid]
                    # self.vp_dca = daily_output['delta_cveg'][1][self.vp_lsid]
                    # self.vp_dcf = daily_output['delta_cveg'][2][self.vp_lsid]
                    self.vp_sto = daily_output['stodbg'][:, self.vp_lsid]
                    self.sp_uptk_costs = daily_output['npp2pay'][self.vp_lsid]

                # UPDATE STATE VARIABLES
                # WATER CWM
                self.runom[step] = self.swp._update_pool(
                    prec[step], daily_output['evavg'])
                self.swp.w1 = np.float64(
                    0.0) if self.swp.w1 < 0.0 else self.swp.w1
                self.swp.w2 = np.float64(
                    0.0) if self.swp.w2 < 0.0 else self.swp.w2
                self.wp_water_upper_mm = self.swp.w1
                self.wp_water_lower_mm = self.swp.w2

                # Plant uptake and Carbon costs of nutrient uptake
                self.nupt[:, step] = daily_output['nupt']
                self.pupt[:, step] = daily_output['pupt']

                # CWM of STORAGE_POOL
                for i in range(3):
                    self.storage_pool[i, step] = np.sum(
                        self.vp_ocp * self.vp_sto[i])

                # OUTPUTS for SOIL CWM
                self.litter_l[step] = daily_output['litter_l'] + \
                    daily_output['cp'][3]
                self.cwd[step] = daily_output['cwd']
                self.litter_fr[step] = daily_output['litter_fr']
                self.lnc[:, step] = daily_output['lnc']
                wtot = self.wp_water_upper_mm + self.wp_water_lower_mm
                s_out = soil_dec.carbon3(self.soil_temp, wtot / self.wmax_mm, self.litter_l[step],
                                         self.cwd[step], self.litter_fr[step], self.lnc[:, step],
                                         self.sp_csoil, self.sp_snc)

                soil_out = catch_out_carbon3(s_out)

                # Organic C N & P
                self.sp_csoil = soil_out['cs']
                self.sp_snc = soil_out['snc']
                idx = np.where(self.sp_snc < 0.0)[0]
                if len(idx) > 0:
                    for i in idx:
                        self.sp_snc[i] = 0.0

                # IF NUTRICYCLE:
                if nutri_cycle:
                    # UPDATE ORGANIC POOLS
                    self.sp_organic_n = self.sp_snc[:2].sum()
                    self.sp_sorganic_n = self.sp_snc[2:4].sum()
                    self.sp_organic_p = self.sp_snc[4:6].sum()
                    self.sp_sorganic_p = self.sp_snc[6:].sum()
                    self.sp_available_p += soil_out['pmin']
                    self.sp_available_n += soil_out['nmin']
                    # NUTRIENT DINAMICS
                    # Inorganic N
                    self.sp_in_n += self.sp_available_n + self.sp_so_n
                    self.sp_so_n = soil_dec.sorbed_n_equil(self.sp_in_n)
                    self.sp_available_n = soil_dec.solution_n_equil(
                        self.sp_in_n)
                    self.sp_in_n -= self.sp_so_n + self.sp_available_n

                    # Inorganic P
                    self.sp_in_p += self.sp_available_p + self.sp_so_p
                    self.sp_so_p = soil_dec.sorbed_p_equil(self.sp_in_p)
                    self.sp_available_p = soil_dec.solution_p_equil(
                        self.sp_in_p)
                    self.sp_in_p -= self.sp_so_p + self.sp_available_p

                    # Sorbed P
                    if self.pupt[1, step] > 0.75:
                        rwarn(
                            f"Puptk_SO > soP_max - 987 | in spin{s}, step{step} - {self.pupt[1, step]}")
                        self.pupt[1, step] = 0.0

                    if self.pupt[1, step] > self.sp_so_p:
                        rwarn(
                            f"Puptk_SO > soP_pool - 992 | in spin{s}, step{step} - {self.pupt[1, step]}")

                    self.sp_so_p -= self.pupt[1, step]

                    try:
                        t1 = np.all(self.sp_snc > 0.0)
                    except:
                        if self.sp_snc is None:
                            self.sp_snc = np.zeros(shape=8,)
                            t1 = True
                        elif self.sp_snc is not None:
                            t1 = True
                        rwarn(f"Exception while handling sp_snc pool")
                    if not t1:
                        self.sp_snc[np.where(self.sp_snc < 0)[0]] = 0.0
                    # ORGANIC nutrients uptake
                    # N
                    if self.nupt[1, step] < 0.0:
                        rwarn(
                            f"NuptkO < 0 - 1003 | in spin{s}, step{step} - {self.nupt[1, step]}")
                        self.nupt[1, step] = 0.0
                    if self.nupt[1, step] > 2.5:
                        rwarn(
                            f"NuptkO  > max - 1007 | in spin{s}, step{step} - {self.nupt[1, step]}")
                        self.nupt[1, step] = 0.0

                    total_on = self.sp_snc[:4].sum()

                    if total_on > 0.0:
                        frsn = [i / total_on for i in self.sp_snc[:4]]
                    else:
                        frsn = [0.0, 0.0, 0.0, 0.0]

                    for i, fr in enumerate(frsn):
                        self.sp_snc[i] -= self.nupt[1, step] * fr

                    idx = np.where(self.sp_snc < 0.0)[0]
                    if len(idx) > 0:
                        for i in idx:
                            self.sp_snc[i] = 0.0

                    self.sp_organic_n = self.sp_snc[:2].sum()
                    self.sp_sorganic_n = self.sp_snc[2:4].sum()

                    # P
                    if self.pupt[2, step] < 0.0:
                        rwarn(
                            f"PuptkO < 0 - 1020 | in spin{s}, step{step} - {self.pupt[2, step]}")
                        self.pupt[2, step] = 0.0
                    if self.pupt[2, step] > 1.0:
                        rwarn(
                            f"PuptkO  > max - 1024 | in spin{s}, step{step} - {self.pupt[2, step]}")
                        self.pupt[2, step] = 0.0
                    total_op = self.sp_snc[4:].sum()
                    if total_op > 0.0:
                        frsp = [i / total_op for i in self.sp_snc[4:]]
                    else:
                        frsp = [0.0, 0.0, 0.0, 0.0]
                    for i, fr in enumerate(frsp):
                        self.sp_snc[i + 4] -= self.pupt[2, step] * fr

                    idx = np.where(self.sp_snc < 0.0)[0]
                    if len(idx) > 0:
                        for i in idx:
                            self.sp_snc[i] = 0.0

                    self.sp_organic_p = self.sp_snc[4:6].sum()
                    self.sp_sorganic_p = self.sp_snc[6:].sum()

                    # Raise some warnings
                    if self.sp_organic_n < 0.0:
                        self.sp_organic_n = 0.0
                        rwarn(f"ON negative in spin{s}, step{step}")
                    if self.sp_sorganic_n < 0.0:
                        self.sp_sorganic_n = 0.0
                        rwarn(f"SON negative in spin{s}, step{step}")
                    if self.sp_organic_p < 0.0:
                        self.sp_organic_p = 0.0
                        rwarn(f"OP negative in spin{s}, step{step}")
                    if self.sp_sorganic_p < 0.0:
                        self.sp_sorganic_p = 0.0
                        rwarn(f"SOP negative in spin{s}, step{step}")

                    # CALCULATE THE EQUILIBTIUM IN SOIL POOLS
                    # Soluble and inorganic pools
                    if self.pupt[0, step] > 1e2:
                        rwarn(
                            f"Puptk > max - 786 | in spin{s}, step{step} - {self.pupt[0, step]}")
                        self.pupt[0, step] = 0.0
                    self.sp_available_p -= self.pupt[0, step]

                    if self.nupt[0, step] > 1e3:
                        rwarn(
                            f"Nuptk > max - 792 | in spin{s}, step{step} - {self.nupt[0, step]}")
                        self.nupt[0, step] = 0.0
                    self.sp_available_n -= self.nupt[0, step]

                # END SOIL NUTRIENT DYNAMICS

                # # #  store (np.array) outputs
                if save:
                    assert self.save == True
                    self.carbon_costs[step] = daily_output['c_cost_cwm']
                    self.emaxm.append(daily_output['epavg'])
                    self.tsoil.append(self.soil_temp)
                    self.photo[step] = daily_output['phavg']
                    self.aresp[step] = daily_output['aravg']
                    self.npp[step] = daily_output['nppavg']
                    self.lai[step] = daily_output['laiavg']
                    self.rcm[step] = daily_output['rcavg']
                    self.f5[step] = daily_output['f5avg']
                    self.evapm[step] = daily_output['evavg']
                    self.wsoil[step] = self.wp_water_upper_mm
                    self.swsoil[step] = self.wp_water_lower_mm
                    self.rm[step] = daily_output['rmavg']
                    self.rg[step] = daily_output['rgavg']
                    self.wue[step] = daily_output['wueavg']
                    self.cue[step] = daily_output['cueavg']
                    self.cdef[step] = daily_output['c_defavg']
                    self.vcmax[step] = daily_output['vcmax']
                    self.specific_la[step] = daily_output['specific_la']
                    self.cleaf[step] = daily_output['cp'][0]
                    self.cawood[step] = daily_output['cp'][1]
                    self.cfroot[step] = daily_output['cp'][2]
                    self.hresp[step] = soil_out['hr']
                    self.csoil[:, step] = soil_out['cs']
                    self.inorg_n[step] = self.sp_in_n
                    self.inorg_p[step] = self.sp_in_p
                    self.sorbed_n[step] = self.sp_so_n
                    self.sorbed_p[step] = self.sp_so_p
                    self.snc[:, step] = soil_out['snc']
                    self.nmin[step] = self.sp_available_n
                    self.pmin[step] = self.sp_available_p
                    self.area[self.vp_lsid, step] = self.vp_ocp
                    self.lim_status[:, self.vp_lsid,
                                    step] = daily_output['limitation_status'][:, self.vp_lsid]
                    self.uptake_strategy[:, self.vp_lsid,
                                         step] = daily_output['uptk_strat'][:, self.vp_lsid]
                if ABORT:
                    rwarn("NO LIVING PLS - ABORT")
            if save:
                if s > 0:
                    while True:
                        if sv.is_alive():
                            sleep(0.5)
                        else:
                            break

                self.flush_data = self._flush_output(
                    'spin', (start_index, end_index))
                sv = Thread(target=self._save_output, args=(self.flush_data,))
                sv.start()
        if save:
            while True:
                if sv.is_alive():
                    sleep(0.5)
                else:
                    break
        return None

    def bdg_spinup(self, start_date, end_date):
        """SPINUP SOIL POOLS - generate soil OM and Organic nutrients inputs for soil spinup
        - Side effect - Start soil water pools pools """

        assert self.filled, "The gridcell has no input data"
        self.budget_spinup = True

        if self.plot:
            splitter = ","
        else:
            splitter = "\t"

        def find_co2(year):
            for i in self.co2_data:
                if int(i.split(splitter)[0]) == year:
                    return float(i.split(splitter)[1].strip())

        def find_index(start, end):
            result = []
            num = np.arange(self.ssize)
            ind = np.arange(self.sind, self.eind + 1)
            for r, i in zip(num, ind):
                if i == start:
                    result.append(r)
            for r, i in zip(num, ind):
                if i == end:
                    result.append(r)
            return result

        # Define start and end dates
        start = cftime.real_datetime(int(start_date[:4]), int(
            start_date[4:6]), int(start_date[6:]))
        end = cftime.real_datetime(int(end_date[:4]), int(
            end_date[4:6]), int(end_date[6:]))
        # Check dates sanity
        assert start < end, "start > end"
        assert start >= self.start_date
        assert end <= self.end_date

        # Define time index
        start_index = int(cftime.date2num(
            start, self.time_unit, self.calendar))
        end_index = int(cftime.date2num(end, self.time_unit, self.calendar))

        lb, hb = find_index(start_index, end_index)
        steps = np.arange(lb, hb + 1)
        day_indexes = np.arange(start_index, end_index + 1)

        # Catch climatic input and make conversions
        temp = self.tas[lb: hb + 1] - 273.15  # ! K to °C
        prec = self.pr[lb: hb + 1] * 86400  # kg m-2 s-1 to  mm/day
        # transforamando de Pascal pra mbar (hPa)
        p_atm = self.ps[lb: hb + 1] * 0.01
        # W m-2 to mol m-2 s-1 ! 0.5 converts RSDS to PAR
        ipar = self.rsds[lb: hb + 1] * 0.5 / 2.18e5
        ru = self.rhs[lb: hb + 1] / 100.0

        year0 = start.year
        co2 = find_co2(year0)
        count_days = start.dayofyr - 2
        loop = 0
        next_year = 0
        wo = []
        llo = []
        cwdo = []
        rlo = []
        lnco = []

        sto = self.vp_sto
        cleaf = self.vp_cleaf
        cwood = self.vp_cwood
        croot = self.vp_croot
        dcl = self.vp_dcl
        dca = self.vp_dca
        dcf = self.vp_dcf
        uptk_costs = np.zeros(npls, order='F')

        for step in range(steps.size):
            loop += 1
            count_days += 1
            # CAST CO2 ATM CONCENTRATION
            days = 366 if m.leap(year0) == 1 else 365
            if count_days == days:
                count_days = 0
                year0 = cftime.num2date(day_indexes[step],
                                        self.time_unit, self.calendar).year
                co2 = find_co2(year0)
                next_year = (find_co2(year0 + 1) - co2) / days

            elif loop == 1 and count_days < days:
                year0 = start.year
                next_year = (find_co2(year0 + 1) - co2) / \
                    (days - count_days)

            co2 += next_year
            self.soil_temp = st.soil_temp(self.soil_temp, temp[step])

            out = model.daily_budget(self.pls_table, self.wp_water_upper_mm, self.wp_water_lower_mm,
                                     self.soil_temp, temp[step], p_atm[step],
                                     ipar[step], ru[step], self.sp_available_n, self.sp_available_p,
                                     self.sp_snc[:4].sum(
                                     ), self.sp_so_p, self.sp_snc[4:].sum(),
                                     co2, sto, cleaf, cwood, croot, uptk_costs, self.wmax_mm)

            # Create a dict with the function output
            daily_output = catch_out_budget(out)
            runoff = self.swp._update_pool(prec[step], daily_output['evavg'])

            self.wp_water_upper_mm = self.swp.w1
            self.wp_water_lower_mm = self.swp.w2
            # UPDATE vegetation pools

            wo.append(np.float64(self.wp_water_upper_mm + self.wp_water_lower_mm))
            llo.append(daily_output['litter_l'])
            cwdo.append(daily_output['cwd'])
            rlo.append(daily_output['litter_fr'])
            lnco.append(daily_output['lnc'])

        f = np.array
        def x(a): return a * 1.0

        return x(f(wo).mean()), x(f(llo).mean()), x(f(cwdo).mean()), x(f(rlo).mean()), x(f(lnco).mean(axis=0,))

    def sdc_spinup(self, water, ll, cwd, rl, lnc):
        """SOIL POOLS SPINUP"""

        for x in range(3000):

            s_out = soil_dec.carbon3(self.soil_temp, water / self.wmax_mm, ll, cwd, rl, lnc,
                                     self.sp_csoil, self.sp_snc)

            soil_out = catch_out_carbon3(s_out)
            self.sp_csoil = soil_out['cs']
            self.sp_snc = soil_out['snc']


class plot(grd):
    """i and j are the latitude and longitude (in that order) of plot location in decimal degrees"""

    def __init__(self, latitude, longitude, dump_folder):
        y, x = find_indices(latitude, longitude, res=0.5)
        super().__init__(x, y, dump_folder)

        self.plot = True

    def init_plot(self, sdata, stime_i, co2, pls_table, tsoil, ssoil, hsoil):
        """ PREPARE A GRIDCELL TO RUN With PLOT OBSERVED DATA
            sdata : python dict with the proper structure - see the input files e.g. CAETE-DVM/input/central/input_data_175-235.pbz2
            stime_i:  python dict with the proper structure - see the input files e.g. CAETE-DVM/input/central/ISIMIP_HISTORICAL_METADATA.pbz2
            These dicts are build upon .csv climatic data in the file CAETE-DVM/src/k34_experiment.py where you can find an application of the plot class
            co2: (list) a alist (association list) with yearly cCO2 ATM data(yyyy\t[CO2]\n)
            pls_table: np.ndarray with functional traits of a set of PLant life strategies
            tsoil, ssoil, hsoil: numpy arrays with soil parameters see the file CAETE-DVM/src/k34_experiment.py
        """

        assert self.filled == False, "already done"

        self.data = sdata

        os.makedirs(self.out_dir, exist_ok=True)
        self.flush_data = 0

        self.pr = self.data['pr']
        self.ps = self.data['ps']
        self.rsds = self.data['rsds']
        self.tas = self.data['tas']
        self.rhs = self.data['hurs']

        # SOIL AND NUTRIENTS
        self.input_nut = []
        self.nutlist = ['tn', 'tp', 'ap', 'ip', 'op']
        for nut in self.nutlist:
            self.input_nut.append(self.data[nut])
        self.soil_dict = dict(zip(self.nutlist, self.input_nut))
        self.data = None

        # TIME
        self.stime = copy.deepcopy(stime_i)
        self.calendar = self.stime['calendar']
        self.time_index = self.stime['time_index']
        self.time_unit = self.stime['units']
        self.ssize = self.time_index.size
        self.sind = int(self.time_index[0])
        self.eind = int(self.time_index[-1])
        self.start_date = cftime.num2date(
            self.time_index[0], self.time_unit, calendar=self.calendar)
        self.end_date = cftime.num2date(
            self.time_index[-1], self.time_unit, calendar=self.calendar)

        # OTHER INPUTS
        self.pls_table = pls_table.copy()
        # self.neighbours = neighbours_index(self.pos, mask)
        self.soil_temp = st.soil_temp_sub(self.tas[:1095] - 273.15)

        # Prepare co2 inputs (we have annually means)
        self.co2_data = copy.deepcopy(co2)

        self.tsoil = []
        self.emaxm = []

        # STATE
        # Water
        self.ws1 = tsoil[0][self.y, self.x].copy()
        self.fc1 = tsoil[1][self.y, self.x].copy()
        self.wp1 = tsoil[2][self.y, self.x].copy()
        self.ws2 = ssoil[0][self.y, self.x].copy()
        self.fc2 = ssoil[1][self.y, self.x].copy()
        self.wp2 = ssoil[2][self.y, self.x].copy()

        self.swp = soil_water(self.ws1, self.ws2, self.fc1, self.fc2, self.wp1, self.wp2)
        self.wp_water_upper_mm = self.swp.w1
        self.wp_water_lower_mm = self.swp.w2
        self.wmax_mm = self.swp.w1_max + self.swp.w2_max

        self.theta_sat = hsoil[0][self.y, self.x].copy()
        self.psi_sat = hsoil[1][self.y, self.x].copy()
        self.soil_texture = hsoil[2][self.y, self.x].copy()

        # Biomass
        self.vp_cleaf = np.zeros(shape=(npls,), order='F') + 0.3
        self.vp_croot = np.zeros(shape=(npls,), order='F') + 0.3
        self.vp_cwood = np.zeros(shape=(npls,), order='F') + 0.01
        self.vp_cwood[pls_table[6,:] == 0.0] = 0.0

        a, b, c, d = m.pft_area_frac(
            self.vp_cleaf, self.vp_croot, self.vp_cwood, self.pls_table[6, :])
        self.vp_lsid = np.where(a > 0.0)[0]
        self.ls = self.vp_lsid.size
        del a, b, c, d
        self.vp_dcl = np.zeros(shape=(npls,), order='F')
        self.vp_dca = np.zeros(shape=(npls,), order='F')
        self.vp_dcf = np.zeros(shape=(npls,), order='F')
        self.vp_ocp = np.zeros(shape=(npls,), order='F')
        self.vp_sto = np.zeros(shape=(3, npls), order='F')

        # # # SOIL
        self.sp_csoil = np.zeros(shape=(4,), order='F') + 1.0
        self.sp_snc = np.zeros(shape=(8,), order='F') + 0.1
        self.sp_available_p = self.soil_dict['ap']
        self.sp_available_n = 0.2 * self.soil_dict['tn']
        self.sp_in_n = 0.4 * self.soil_dict['tn']
        self.sp_so_n = 0.2 * self.soil_dict['tn']
        self.sp_so_p = self.soil_dict['tp'] - sum(self.input_nut[2:])
        self.sp_in_p = self.soil_dict['ip']
        self.sp_uptk_costs = np.zeros(npls, order='F')
        self.sp_organic_n = 0.1 * self.soil_dict['tn']
        self.sp_sorganic_n = 0.1 * self.soil_dict['tn']
        self.sp_organic_p = 0.5 * self.soil_dict['op']
        self.sp_sorganic_p = self.soil_dict['op'] - self.sp_organic_p

        self.outputs = dict()
        self.filled = True

        return None


if __name__ == '__main__':

    # Short example of how to run the model. Also used to do some profiling

    from metacommunity import read_pls_table
    from parameters import *

    # # Read CO2 data
    co2_path = Path("../input/co2/historical_CO2_annual_1765_2018.txt")
    main_table = read_pls_table(Path("./PLS_MAIN/pls_attrs-25000.csv"))


    r = region("region_test",
                   "../input/test_input",
                   (tsoil, ssoil, hsoil),
                   co2_path,
                   main_table)

    c = r.set_gridcells()

    gridcell = r[0]

    prof = 0
    if prof:
        import cProfile
        command = "gridcell.run_gridcell('1901-01-01', '1901-12-31', spinup=0, fixed_co2_atm_conc=1901, save=False, nutri_cycle=True, reset_community=True)"
        cProfile.run(command, sort="cumulative", filename="profile.prof")

    else:
        run_result = gridcell.run_gridcell("1901-01-01", "1930-12-31", spinup=3, fixed_co2_atm_conc=None,
                                       save=False, nutri_cycle=True, reset_community=True, kill_and_reset=True)
        comm = gridcell.metacomm[0]
