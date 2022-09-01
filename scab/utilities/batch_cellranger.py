#!/usr/bin/env python
# filename: batch_cellranger.py


#
# Copyright (c) 2022 Bryan Briney
# License: The MIT license (http://opensource.org/licenses/MIT)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software
# and associated documentation files (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
# BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#


from argparse import ArgumentParser
import csv
import os
import shutil
import subprocess as sp
import sys
import time
from unicodedata import name
import urllib

import yaml

from natsort import natsorted

from sample_sheet import SampleSheet

from abutils.utils import log
from abutils.utils.pipeline import list_files, make_dir


from ..version import __version__


def parse_arguments(print_help=False):
    parser = ArgumentParser(prog='batch_cellranger', description="Batch CellRanger processing of one or more 10x Genomics samples.")
    parser.add_argument('-p', '--project-directory', dest='project_dir',  required=True,
                        help="The project directory, where run data will be downloaded \
                        and output files will be written. Required.")
    parser.add_argument('-c', '--config-file', dest='config_file', required=True,
                        help="The config file, in YML format. Required.")
    parser.add_argument('-d', '--debug', dest="debug", action='store_true', default=False,
                        help="If set, logs are much more detailed, including the stdout and stderr \
                        from all commands. Default is ``False``, which produces more consise logs.")
    parser.add_argument('-v', '--version', action='version', \
                        version='%(prog)s {version}'.format(version=__version__))
    if print_help:
        parser.print_help()
    else:
        args = parser.parse_args()
        args.project_dir = os.path.abspath(args.project_dir)
        return args


class Args():
    def __init__(self, project_dir=None, config_file=None, debug=False):
        super(Args, self).__init__()
        self.project_dir = os.path.abspath(project_dir)
        self.config_file = os.path.abspath(config_file)
        self.debug = debug


class Config():
    '''
    ``Config`` provides the following attributes:

      - ``config_file``: path to the configuration file, in YAML format.
      - ``runs``: a list of ``Run`` objects
      - ``samples``: a list of `Sample` objects. Samples are parsed from `Run`s, so
                   samples may exist in this list that will not be processed by any
                   of the cellranger operations.
      - ``ops``: a dictionary with cellranger operations (count, vdj, aggr or features)
               as keys and a list of subjects as values. Maps the operation with
               the samples on which the operation will be performed.
      - ``reference``: dictionary mapping sample names to the VDJ reference. Must include
                     a ``default`` reference, which will be used for all subjects not
                     specifically named in the dictionary.
      - ``transcriptome``: same as ``reference``, but mapping samples to a reference
                         transcriptome for ``cellranger count`` operations.
      - ``feature_reference``: same as ``reference``, but mapping samples to a 
                             feature reference.
      - ``uiport``: port for the cellranger UI. Default is 72647.
      - ``cellranger``: path to the cellranger binary. Default is "cellranger", which
                      assumes that the cellranger binary is on your PATH.

    '''
    def __init__(self, config_file):
        self.config_file = os.path.abspath(config_file)
        self.gex_reference = None
        self.vdj_reference = None
        self.feature_reference = None
        self.uiport = None
        self.cellranger = None
        self._runs = None
        self._samples = None
        self._parse_config_file()
        
    def __repr__(self):
        rlist = ['BATCH CELLRANGER CONFIGURATION']
        rlist.append('------------------------------')
        rlist.append('config file: {}'.format(self.config_file))
        rlist.append('VDJ reference:')
        rlist.append('  - default: {}'.format(self.reference['default']))
        for k, v in self.reference.items():
            if k == 'default':
                continue
            rlist.append('  - {}: {}'.format(k, v))
        rlist.append('transcriptome:')
        rlist.append('  - default: {}'.format(self.transcriptome['default']))
        for k, v in self.transcriptome.items():
            if k == 'default':
                continue
            rlist.append('  - {}: {}'.format(k, v))
        rlist.append('feature reference:')
        rlist.append('  - default: {}'.format(self.feature_reference['default']))
        for k, v in self.feature_reference.items():
            if k == 'default':
                continue
            rlist.append('  - {}: {}'.format(k, v))
        rlist.append('UI port: {}'.format(self.uiport))
        rlist.append('cellranger binary: {}'.format(self.cellranger))
        rlist.append('runs: {}'.format([r.name for r in self.runs]))
        rlist.append('samples: {}'.format([s.name for s in self.samples]))
        rlist.append('operations:')
        rlist.append('  - vdj: {}'.format(self.ops.get('vdj', [])))
        rlist.append('  - count: {}'.format(self.ops.get('count', [])))
        # rlist.append('  - features: {}'.format(self.ops.get('features', [])))
        rlist.append('  - aggr:')
        for k, v in self.ops.get('aggr', {}).items():
            rlist.append('    - {}: {}'.format(k, v))
        return '\n'.join(rlist)

    
    @property
    def runs(self):
        if self._runs is None:
            return []
        return self._runs

    @runs.setter
    def runs(self, runs):
        self._runs = runs

    
    @property
    def samples(self):
        if self._samples is None:
            return []
        return self._samples

    @samples.setter
    def samples(self, samples):
        self._samples = samples

    
    @property
    def ops(self):
        if self._ops is None:
            return []
        return self._ops

    @ops.setter
    def ops(self, ops):
        self._ops = ops


    def _parse_config_file(self):
        with open(self.config_file) as f:
            config = yaml.safe_load(f)
        # runs
        self.runs = [Run(name, cfg) for name, cfg in config['runs'].items()]
        # references
        self.gex_reference = config.get('gex_reference', {})
        self.vdj_reference = config.get('vdj_reference', {})
        self.feature_reference = config.get('feature_reference', {})
        # samples
        self.samples = [
            Sample(
                name, 
                lib_dict,
                gex_reference=self.gex_reference,
                vdj_reference=self.vdj_reference,
                feature_reference=self.feature_reference
            ) 
            for name, lib_dict in config['samples'].items()
        ]
         # general config options
        self.uiport = config.get('uiport', 72647)
        self.cellranger = config.get('cellranger', 'cellranger')

            # for name, lib_dict in config['samples'].itmes()
        # collect samples from runs
        # libraries= []
        # for run in self.runs:
        #     if run.libraries is not None:
        #         libraries += run.libraries
        # self.libraries = list(set(libraries))
        # # # assign runs to each sample:
        # # for run in self.runs:
        # #     for s in samples:
        # #         if s.name in [s.name for s in run.samples]:
        # #             s.add_run(run.name)
        # # parse ops
        # self.ops = {}
        # self.ops['vdj'] = config.get('vdj', [])
        # self.ops['count'] = config.get('count', {})
        # self.ops['aggr'] = config.get('aggr', {})
        # # assign ops to each sample
        # for op, samples in self.ops.items():
        #     if op in ['count']:
        #         samples = [k for subject_dict in samples for k in subject_dict.keys()]
        #     for s in self.samples:
        #         if s.name in samples:
        #             s.add_op(op)
        
        # assign references/transcriptomes to each sample:
        # for s in self.samples:
        #     s.gex_reference = config['gex_reference'].get(s.name, config['gex_reference']['default'])
        #     s.vdj_reference = config['vdj_reference'].get(s.name, config['vdj_reference']['default'])
        #     s.feature_reference = config['feature_reference'].get(s.name, config['feature_reference']['default'])
        # # general config options
        # self.uiport = config.get('uiport', 72647)
        # self.cellranger = config.get('cellranger', 'cellranger')



class Run():
    '''
    Object for aggregation of sequencing run information throughput the 10x processing
    '''
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.url = config.get('url', None)
        self.path = os.path.abspath(config['path']) if 'path' in config else None
        self.is_compressed = config.get('is_compressed', True)
        self.samplesheet = os.path.abspath(config['samplesheet']) if 'samplesheet' in config else None
        self.simple_csv = os.path.abspath(config['simple_csv']) if 'simple_csv' in config else None
        self.copy_to_project = config.get('copy_to_project', False)
        self._fastq_path = None
        self._libraries = None

    def __repr__(self):
        rstring = 'RUN: {}'.format(self.name)
        rlist = [rstring]
        rlist.append('-' * len(rstring))
        if self.url is not None:
            rlist.append('url: {}'.format(self.url))
        if self.path is not None:
            rlist.append('path: {}'.format(self.path))
        rlist.append('compressed: {}'.format(self.is_compressed))
        if self.samplesheet is not None:
            rlist.append('samplesheet: {}'.format(self.samplesheet))
        if self.simple_csv is not None:
            rlist.append('simple csv: {}'.format(self.simple_csv))
        rlist.append('fastq path: {}'.format(self.fastq_path))
        rlist.append('samples: {}'.format(self.samples))
        return '\n'.join(rlist)

    
    @property
    def sample_names(self):
        if self.samples is not None:
            return [s.name for s in self.samples]
        return []


    @property
    def fastq_path(self):
        return self._fastq_path

    @fastq_path.setter
    def fastq_path(self, path):
        self._fastq_path = path


    @property
    def libraries(self):
        if self._libraries is None:
            self._libraries = self._parse_libraries()
        return self._libraries

    @libraries.setter
    def libraries(self, libraries):
        self._libraries = libraries


    def print_splash(self):
        l = len(self.name)
        logger.info('')
        logger.info('-' * (l + 4))
        logger.info('  ' + self.name)
        logger.info('-' * (l + 4))


    def get(self, raw_dir, log_dir=None, debug=None):
        destination = os.path.join(os.path.abspath(raw_dir), self.name)
        if all([self.path is not None, self.copy_to_project, not self.is_compressed]):
            self.path = self._copy(destination, log_dir=log_dir, debug=debug)
        if self.url is not None:
            self.path = self._download(self.url, destination, log_dir=log_dir, debug=debug)
        if self.is_compressed:
            self.path = self._decompress(self.path, destination, log_dir=log_dir, debug=debug)


    def mkfastq(self, fastq_dir, cellranger='cellranger', uiport=None, log_dir=None, debug=None):
        logger.info('Running mkfastq....')
        # fastq_dir = os.path.abspath(fastq_dir)
        # make_dir(fastq_dir)
        mkfastq_cmd = f"cd '{fastq_dir}' && {cellranger} mkfastq"
        mkfastq_cmd += f" --id={self.name}"
        mkfastq_cmd += f" --run='{self.path}'"
        if self.samplesheet is not None:
            mkfastq_cmd += f" --samplesheet='{self.samplesheet}'"
        else:
            mkfastq_cmd += f" --csv='{self.simple_csv}'"
        if uiport is not None:
            mkfastq_cmd += f" --uiport={uiport}"
        p = sp.Popen(mkfastq_cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
        time.sleep(5)
        uifile = os.path.join(fastq_dir, f'{self.name}/_uiport')
        with open(uifile) as f:
            uistring = f.read().strip()
        external_ip = urllib.request.urlopen('https://api.ipify.org').read().decode('utf8')
        uistring = f"http://{external_ip}:{uistring.split(':')[-1]}"
        logger.info(f'UI is at {uistring}')
        o, e = p.communicate()
        if debug:
            logger.info('\nMKFASTQ')
            logger.info(mkfastq_cmd)
            logger.info(o)
            logger.info(e)
            logger.info('\n')
        if log_dir is not None:
            log_subdir = os.path.join(log_dir, 'mkfastq')
            make_dir(log_subdir)
            write_log(self.name, log_subdir, stdout=o, stderr=e)
        # logger.info('done')
        ## NEED TO DOUBLE-CHECK WHAT THE FASTQ PATH ACTUALLY IS
        ## is it just --output-dir? or do they go into an --id subfolder?
        self.fastq_path = os.path.join(fastq_dir, f'{self.name}/outs/fastq_path')
        return self.fastq_path


    def _copy(self, destination, log_dir=None, debug=False):
        shutil.copytree(self.path, destination)
        return destination
    

    def _download(self, url, destination, log_dir=None, debug=False):
        logger.info('Downloading run data....')
        destination = os.path.abspath(destination)
        make_dir(destination)
        wget_cmd = "wget {} '{}'".format(url, destination)
        p = sp.Popen(wget_cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
        o, e = p.communicate()
        if debug:
            logger.info('\nDOWNLOAD')
            logger.info(wget_cmd)
            logger.info(o)
            logger.info(e)
            logger.info('\n')
        if log_dir is not None:
            log_subdir = os.path.join(log_dir, 'download')
            make_dir(log_subdir)
            write_log(self.name, log_subdir, stdout=o, stderr=e)
        fname = os.path.basename(url)
        # logger.info('done')
        return os.path.join(destination, fname)


    def _decompress(self, source, destination, log_dir=None, debug=False):
        logger.info('Decompressing run data....')
        source = os.path.abspath(source)
        destination = os.path.abspath(destination)
        make_dir(destination)
        if source.endswith(('.tar.gz', '.tgz')):
            cmd = f"tar xzvf '{source}' -C '{destination}'"
        elif source.endswith('.tar'):
            cmd = f"tar xvf '{source}' -C '{destination}'"
        elif source.endswith('.zip'):
            cmd = f"unzip {source} -d {destination}"
        else:
            err = f'ERROR: input file {source} has an unsupported compression type. ' 
            err += 'Only files with .tar, .tar.gz, .tgz or .zip extensions are supported.'
            print(err)
            sys.exit()
        p = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
        o, e = p.communicate()
        if debug:
            logger.info('\nDECOMPRESS')
            logger.info(cmd)
            logger.info(o)
            logger.info(e)
            logger.info('\n')
        if log_dir is not None:
            log_subdir = os.path.join(log_dir, 'decompress')
            make_dir(log_subdir)
            write_log(self.name, log_subdir, stdout=o, stderr=e)
        run_dir = destination
        for (root, subdirs, files) in os.walk(destination):
            if 'RTAComplete.txt' in files:
                run_dir = os.path.join(destination, root)
                break
        # logger.info('done')
        return run_dir

    
    def _parse_libraries(self):
        if self.samplesheet is not None:
            return self._parse_samplesheet()
        if self.simple_csv is not None:
            return self._parse_simple_csv()

    
    def _parse_samplesheet(self):
        ss = SampleSheet(self.samplesheet)
        samples = []
        for s in ss.samples:
            samples.append(Sample(s.Sample_ID, name=s.Sample_Name, index=s.index))
        return samples


    def _parse_simple_csv(self):
        samples = []
        with open(self.simple_csv) as csvfile:
            reader = csv.DictReader(csvfile)
            for r in reader:
                samples.append(Sample(r['Sample'], index=r['Index']))
        return samples



class Sample():
    '''
    Object for aggregating information about a single sample
    '''
    def __init__(
        self, 
        name, 
        library_dict,
        gex_reference=None, 
        vdj_reference=None, 
        feature_reference=None, 
    ):
        self.name = name
        self.gex_reference = gex_reference
        self.vdj_reference = vdj_reference
        self.feature_reference = feature_reference
        self._library_dict = library_dict
        self._libraries = None


    def __lt__(self, other):
        return all([self.name < other.name])

    def __hash__(self):
        return hash(self.name)


    @property
    def libraries(self):
        if self._libraries is None:
            self._libraries = []
            for lib_type, name in self._library_dict.items():
                self._libraries.append(Library(name, lib_type))
        return self._libraries


    def make_config_csv(self, csv_path):
        csv_dir = os.path.dirname(csv_path)
        if not os.path.isdir(csv_dir):
            make_dir(csv_dir)
        with open(csv_path, 'w') as f:
            f.write(self._build_config_csv())


    def _build_config_csv(self):
        config = ''
        if self.gex_reference is not None:
            config += '[gene-expression]\n'
            config += f'reference,{self.gex_reference}\n\n'
        if self.vdj_reference is not None:
            config += '[vdj]\n'
            config += f'reference,{self.vdj_reference}\n\n'
        if self.feature_reference is not None:
            config += '[feature]\n'
            config += f'reference,{self.feature_reference}\n\n'
        config += '[libraries]\n'
        config += 'fastq_id,fastqs,feature_types\n'
        for library in self.libraries:
            for fastq in library.fastq_paths:
                config += f'{library.name},{fastq},{library.type}\n'
        return config
                


class Library():
    '''
    Object for aggregating information about a single library
    '''
    def __init__(self, name, library_type):
        self.name = name
        self.type = library_type
        self._fastq_paths = None


    @property
    def fastq_paths(self):
        if self._fastq_paths is None:
            self._fastq_paths = []
        return self._fastq_paths


    def add_fastq_path(self, fastq_path):
        self.fastq_path.append(os.path.abspath(fastq_path))








#==================
#    OPERATIONS
#==================


def cellranger_multi(
    sample, 
    output_dir, 
    cellranger='cellranger', 
    uiport=None, 
    log_dir=None,
    debug=None
):
    logger.info(f'building config CSV...')
    config_csv = os.path.join(output_dir, f"{sample.name}_config.csv")
    sample.make_config_csv(config_csv)
    multi_cmd = f"cd '{output_dir}'"
    multi_cmd += f" && {cellranger} multi --id {sample.name} --csv {config_csv}"
    if uiport is not None:
        multi_cmd += f' --uiport {uiport}'
    logger.info(f'running CellRanger..')
    p = sp.Popen(multi_cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
    time.sleep(3)
    uifile = os.path.join(output_dir, f'{sample.name}/_uiport')
    with open(uifile) as f:
        uistring = f.read().strip()
    external_ip = urllib.request.urlopen('https://api.ipify.org').read().decode('utf8')
    uistring = f"http://{external_ip}:{uistring.split(':')[-1]}"
    logger.info(f'CellRanger UI is at {uistring}')
    o, e = p.communicate()
    if debug:
        logger.info('\CELLRANGER MULTI')
        logger.info(multi_cmd)
        logger.info(o)
        logger.info(e)
        logger.info('\n')
    if log_dir is not None:
        log_subdir = os.path.join(log_dir, 'cellranger_multi')
        make_dir(log_subdir)
        write_log(sample.name, log_subdir, stdout=o, stderr=e)


# op_lookup = {'gex': 'Gene Expression',
#              'features': 'Antibody Capture',
#              'crispr': 'CRISPR Guide Capture',
#              'bcr': 'VDJ-B',
#              'tcr': 'VDJ-T',
#              'tcr-gd': 'VDJ-T-GD'}


# def cellranger_vdj(sample, vdj_dir, cellranger='cellranger', uiport=None, log_dir=None, debug=False):
#     '''
#     docstring
#     '''
#     vdj_dir = os.path.abspath(vdj_dir)
#     vdj_cmd = "cd '{}'".format(vdj_dir)
#     vdj_cmd += " && {} vdj --id {} --sample {} --reference '{}'".format(cellranger,
#                                                                         sample.name,
#                                                                         sample.id,
#                                                                         sample.reference)
#     for fastq in sample.fastqs:
#         vdj_cmd += " --fastq '{}'".format(fastq)
#     if uiport is not None:
#         vdj_cmd += ' --uiport {}'.format(uiport)
#     p = sp.Popen(vdj_cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
#     time.sleep(3)
#     uifile = os.path.join(vdj_dir, '{}/_uiport'.format(self.name))
#     with open(uifile) as f:
#         uistring = f.read().strip()
#     external_ip = urllib.request.urlopen('https://api.ipify.org').read().decode('utf8')
#     uistring = 'http://' + external_ip + ':' + uistring.split(':')[-1]
#     logger.info('CellRanger UI is at {}'.format(uistring))
#     o, e = p.communicate()
#     if debug:
#         logger.info('\nCELLRANGER VDJ')
#         logger.info(o)
#         logger.info(e)
#     if log_dir is not None:
#         log_subdir = os.path.join(log_dir, 'vdj')
#         make_dir(log_subdir)
#         write_log(sample.name, log_subdir, stdout=o, stderr=e)
#     return os.path.join(vdj_dir, sample.name)



# def cellranger_count(group, samples, feature_ref, count_dir,
#                      cellranger='cellranger', uiport=None, log_dir=None, debug=False):
#     '''
#     docstring
#     '''
#     count_dir = os.path.abspath(count_dir)
#     lib_csv = _make_feature_library_csv(samples, group, count_dir)
#     count_cmd = "cd '{}'".format(count_dir)
#     count_cmd += " && {} count --id {} --libraries {} --feature_ref {} --transcriptome '{}'".format(cellranger,
#                                                                                                     lib_csv,
#                                                                                                     feature_ref,
#                                                                                                     sample.id,
#                                                                                                     sample.transcriptome)
#     for fastq in sample.fastqs:
#         count_cmd += " --fastqs '{}'".format(fastq)
#     if uiport is not None:
#         count_cmd += " --uiport '{}'".format(uiport)
#     p = sp.Popen(count_cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
#     time.sleep(3)
#     uifile = os.path.join(count_dir, '{}/_uiport'.format(self.name))
#     with open(uifile) as f:
#         uistring = f.read().strip()
#     external_ip = urllib.request.urlopen('https://api.ipify.org').read().decode('utf8')
#     uistring = 'http://' + external_ip + ':' + uistring.split(':')[-1]
#     logger.info('CellRanger UI is at {}'.format(uistring))
#     o, e = p.communicate()
#     if debug:
#         logger.info('\nCELLRANGER COUNT')
#         logger.info(o)
#         logger.info(e)
#     if log_dir is not None:
#         log_subdir = os.path.join(log_dir, 'count')
#         make_dir(log_subdir)
#         write_log(sample.name, log_subdir, stdout=o, stderr=e)
#     return os.path.join(count_dir, sample.name)


# def cellranger_feature_barcoding(sample, feature_dir, cellranger='cellranger', uiport=None, log_dir=None, debug=False):
#     feature_dir = os.path.abspath(feature_dir)
#     lib_csv = _make_feature_library_csv(sample, feature_dir)
#     feature_cmd = "cd '{}'".format(feature_dir)
#     feature_cmd += " && {} count --id {} --libraries '{}' --feature-ref '{}' --sample {}'.format(cellranger, 
#                                                                                                  sample.name,
#                                                                                                  lib_csv,
#                                                                                                  sample.feature_reference,
#                                                                                                  sample.name)
#     p = sp.Popen(feature_cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
#     time.sleep(3)
#     uifile = os.path.join(feature_dir, '{}/_uiport'.format(self.name))
#     with open(uifile) as f:
#         uistring = f.read().strip()
#     external_ip = urllib.request.urlopen('https://api.ipify.org').read().decode('utf8')
#     uistring = 'http://' + external_ip + ':' + uistring.split(':')[-1]
#     logger.info('CellRanger UI is at {}'.format(uistring))
#     o, e = p.communicate()
#     if debug:
#         logger.info('\nCELLRANGER FEATURES')
#         logger.info(o)
#         logger.info(e)
#     if log_dir is not None:
#         log_subdir = os.path.join(log_dir, 'features')
#         make_dir(log_subdir)
#         write_log(sample.name, log_subdir, stdout=o, stderr=e)
#     return os.path.join(feature_dir, sample.name)


def _make_feature_library_csv(samples, feature_dir):
    lib_str = 'fastqs,sample,library_type\n'
    for sample in samples:
        for fastq in sample.fastqs:
            lib_str += '{},{},{}'.format(fastq, sample.name, sample.op_type)
    lib_path = os.path.join(feature_dir, '{}_feature-library.csv'.format(sample.name))
    with open(lib_path, 'w') as f:
        f.write(lib_str)
    return lib_path


def cellranger_aggr(samples, group, aggr_dir, normalize='mapped', cellranger='cellranger', uiport=None, log_dir=None, debug=False):
    aggr_dir = os.path.abspath(aggr_dir)
    aggr_csv = _make_aggr_csv(samples, aggr_dir)
    aggr_cmd = "cd '{}'".format(aggr_dir)
    aggr_cmd += " && {} count --id {} --csv '{}' --normalize {}".format(cellranger, 
                                                                        group,
                                                                        aggr_csv,
                                                                        normalize)
    ## Eventually want to replace grabbing stdout/stderr with p.communicate(), so we can grab the standard output
    ## in real time, parse out the url for the UI and print to screen so the user can follow along with the UI
    p = sp.Popen(aggr_cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
    o, e = p.communicate()
    if debug:
        logger.info('\nCELLRANGER AGGR')
        logger.info(o)
        logger.info(e)
    if log_dir is not None:
        log_subdir = os.path.join(log_dir, 'aggr')
        make_dir(log_subdir)
        write_log(group, log_subdir, stdout=o, stderr=e)
    return os.path.join(aggr_dir, group)


def _make_aggr_csv(samples, aggr_dir):
    aggr_dir = os.path.join(aggr_dir)
    aggr_csv = os.path.join(aggr_dir, 'aggr.csv')
    lines = ['library_id,molecule_h5', ]
    for sample in samples:
        h5_path = os.path.join(sample.count_path, 'outs/molecule_info.h5')
        lines.append('{},{}'.format(sample.id, h5_path))
    with open(aggr_csv, 'w') as f:
        f.write('\n'.join(lines))
    return aggr_csv






def build_directory_structure(project_dir, cfg):
    dirs = {}
    make_dir(project_dir)
    shutil.copy(cfg.config_file, os.path.join(project_dir, 'config.yaml'))
    dirs['run'] = os.path.join(project_dir, 'run_data')
    dirs['mkfastq'] = os.path.join(project_dir, 'cellranger/mkfastq')
    dirs['multi'] = os.path.join(project_dir, 'cellranger/multi')
    # dirs['vdj'] = os.path.join(project_dir, 'vdj')
    # dirs['count'] = os.path.join(project_dir, 'count')
    # dirs['features'] = os.path.join(project_dir, 'features')
    # dirs['aggr'] = os.path.join(project_dir, 'aggr')
    for op in dirs.keys():
        make_dir(dirs[op])
    dirs['log'] = os.path.join(project_dir, 'logs')
    make_dir(dirs['log'])
    return dirs


def write_log(prefix, dir, stdout=None, stderr=None):
    if stdout is not None:
        stdout_file = os.path.join(dir, '{}.stdout'.format(prefix))
        with open(stdout_file, 'w') as f:
            f.write(stdout)
    if stderr is not None:
        stderr_file = os.path.join(dir, '{}.stderr'.format(prefix))
        with open(stderr_file, 'w') as f:
            f.write(stderr)


def print_plan(cfg):
    '''
    prints the plan (runs, samples, ops, references, etc)
    '''
    pass


def print_op_splash(op, samples):
    pass


def print_aggr_splash(aggr):
    pass






def main(args):
    # parse the config file
    cfg = Config(args.config_file)
    print_plan(cfg)

    # build directory structure
    dirs = build_directory_structure(args.project_dir, cfg)

    # setup logging
    run_log = os.path.join(dirs['log'], 'batch_cellranger.log')
    log.setup_logging(run_log, print_log_location=False, debug=args.debug)
    global logger
    logger = log.get_logger()

    # mkfastq
    for run in cfg.runs:
        run.print_splash()
        run.get(
            dirs['run'], 
            log_dir=dirs['log'], 
            debug=args.debug
        )
        run.mkfastq(
            dirs['mkfastq'],
            cellranger=cfg.cellranger,
            log_dir=dirs['log'],
            debug=args.debug
        )
        for sample in cfg.samples:
            for library in sample.libraries:
                if library.name in run.libraries:
                    library.add_fastq_path(run.fastq_path)

    # cellranger multi
    for sample in cfg.samples:
        logger.info('\n')
        logger.info(sample.name)
        logger.info('-' * len(sample.name))
        config_csv = os.path.join(dirs['multi'], f"{sample.name}_config.csv")
        sample.make_config_csv(config_csv)
        cellranger_multi(
            sample, 
            dirs['multi'],
            cellranger=cfg.cellranger, 
            uiport=cfg.uiport, 
            log_dir=dirs['log'],
            debug=args.debug
        )



    # # operations (except aggr)
    # opmap = {'vdj': cellranger_vdj,
    #          'count': cellranger_count,
    #          'features': cellranger_feature_barcoding}
    
    # for op in ['vdj', 'count', 'features']:
    #     print_op_splash(op)
    #     opfunction = opmap[op]
    #     for sample in cfg.samples:
    #         if op not in sample.ops:
    #             continue
    #         opfunction(sample,
    #                    dirs[op],
    #                    cellranger=cfg.cellranger,
    #                    uiport=cfg.uiport,
    #                    log_dir=dirs['log'],
    #                    debug=args.debug)
    
    # vdj
    print_op_splash('vdj', cfg.samples)
    for sample in cfg.samples:
        if 'vdj' not in sample.ops:
            continue
        path = cellranger_vdj(sample,
                              dirs['vdj'],
                              cellranger=cfg.cellranger,
                              uiport=cfg.uiport,
                              log_dir=dirs['log'],
                              debug=args.debug)
        sample.vdj_path = path

    # count
    print_op_splash('count', cfg.samples)
    for group, sample_dict in cfg.ops['count']:
        samples = [s for s in cfg.samples if s.name in sample_dict]
        for s in samples:
            s.op_type = sample_dict[s.name]
        path = cellranger_count(samples,
                                dirs['count'],
                                cellranger=cfg.cellranger,
                                uiport=cfg.uiport,
                                log_dir=dirs['log'],
                                debug=args.debug)    
        





    for sample in cfg.samples:
        if 'count' not in sample.ops:
            continue
        path = cellranger_count(sample,
                                dirs['count'],
                                cellranger=cfg.cellranger,
                                uiport=cfg.uiport,
                                log_dir=dirs['log'],
                                debug=args.debug)
        sample.count_path = path

    # # features
    # print_op_splash('features', cfg.samples)
    # for sample in cfg.samples:
    #     if 'features' not in sample.ops:
    #         continue
    #     path = cellranger_feature_barcoding(sample,
    #                                         dirs['features'],
    #                                         cellranger=cfg.cellranger,
    #                                         uiport=cfg.uiport,
    #                                         log_dir=dirs['log'],
    #                                         debug=args.debug)
    #     sample.feature_path = path

    # aggr
    print_aggr_splash(cfg.ops['aggr'])
    for group, sample_names in cfg.ops['aggr'].items():
        samples = [s for s in cfg.samples if s.name in sample_names]
        path = cellranger_aggr(samples,
                               group,
                               dirs['aggr'],
                               normalize='mapped',
                               cellranger=cfg.cellranger,
                               uiport=cfg.uiport,
                               log_dir=dirs['log'],
                               debug=args.debug)
        for s in samples:
            s.aggr_path = path
    
    # compress



if __name__ == "__main__":
    args = parse_arguments()
    main(args)




