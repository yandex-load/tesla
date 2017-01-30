# -*- coding: utf-8 -*-

import datetime
import csv
import requests
import pandas as pd
import logging
import numpy as np
import argparse
import json
import re
import sys

from volta.analysis.sync import sync, torch_status

from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


logger = logging.getLogger(__name__)


def WriteListToCSV(csv_file, data_list):
    """
    Write contents of python list to CSV file row-by-row
    FIXME : python csv standart library unable to use unicode. Burn this logic, please!

    Args:
        csv_file: output csv filename
        data_list: python list w/ contents

    Returns:
        None
    """
    try:
        with open(csv_file, 'w') as csvfile:
            writer = csv.writer(csvfile, dialect='excel', quoting=csv.QUOTE_NONNUMERIC)
            for data in data_list:
                writer.writerow(data)
    except IOError as exc:
        print ("I/O error:" % exc)
    return


def CreateJob(test_id, meta, config, task='LOAD-272'):
    """
    creates job in lunapark, uploading metadata

    Args:
        test_id: volta test id, str
        task: lunapark task id, str
        meta: meta json
    Returns:

    """
    try:
        # prod
        url = "https://lunapark.yandex-team.ru/mobile/create_job.json"
        # testing
        # url = "https://lunapark.test.yandex-team.ru/mobile/create_job.json"
        if not meta:
            data = {
                'task': task,
                'test_id': test_id,
            }
        else:
            data = {
                'task': task,
                'test_id': test_id
            }
            for key in meta:
                data[str(key)] = str(meta[key])
        lunapark_req = requests.post(url, data=data, verify=False)
        logger.debug('Lunapark create job status: %s', lunapark_req.status_code)
        logger.debug('Data: %s', data)
        answ = lunapark_req.json()
        logger.debug(answ)
        job_url = 'https://lunapark.yandex-team.ru/mobile/{jobno}'.format(jobno=answ['jobno'])
    except Exception as exc:
        logger.error('Lunapark create job exception: %s', exc, exc_info=True)
        return None
    return job_url




class CurrentsWorker(object):
    def __init__(self, args, sync, date, test_id):
        self.filename = args.get('filename')
        self.sync = sync
        self.date = date
        self.test_id = test_id
        self.output_file = 'current_{test_id}.data'.format(test_id=test_id)
        #self.backend = ('http://volta-backend-test.haze.yandex.net:8123', 'volta.current')
        self.backend = ('https://lunapark.yandex-team.ru/api/volta', 'volta.current')
        #self.backend = ('https://lunapark.test.yandex-team.ru/api/volta', 'volta.current')        
        self.samplerate = args.get('samplerate')
        self.slope = args.get('slope')
        self.offset = args.get('offset')
        self.binary = args.get('binary')

    def FormatCurrent(self):
        """
        lists of currents data

        Returns:
            list of values w/ lists inside, format: [today, test_id, ts, message]
        """
        logger.info('Started formatting currents')
        reader = FileReader(self.filename, self.slope, self.offset)
        if self.binary:
            df = reader.binary_to_df()
        else:
            df = reader.plaintext_to_df()
        start = datetime.datetime.utcfromtimestamp(self.sync)
        index_freq = "{value}{units}".format(
            value = int(10 ** 6 / self.samplerate),
            units = "us"
        )
        logger.debug('Index freq: %s', index_freq)
        index = pd.date_range(start, periods=len(df), freq=index_freq)
        series = pd.Series(df.values, index=index)
        values = []
        for key, value in series.iteritems():
            dt = key.strftime("%Y-%m-%d %H:%M:%S.%f")
            ts = int((datetime.datetime.strptime(dt, "%Y-%m-%d %H:%M:%S.%f") - datetime.datetime(1970,1,1)).total_seconds() * 10000 )
            values.append([self.date, self.test_id, ts, value])
        return values

    def upload(self):
        with open(self.output_file, 'r') as outfile:
            data = outfile.read()
            url = "{url}{query}".format(
                url="{backend_url}/?query=".format(backend_url=self.backend[0]),
                query="INSERT INTO {backend_table} FORMAT CSV".format(backend_table=self.backend[1])
            )
            r = requests.post(url, data=data)
            logger.debug('Upload current to clickhouse status: %s. Message: %s', r.status_code, r.text, exc_info=True)
            r.raise_for_status()
            return


class EventsWorker(object):
    def __init__(self, fname, sync_point, date, test_id):
        self.filename = fname
        self.date = date
        self.sync = sync_point
        self.test_id = test_id
        self.output_file = 'events_{test_id}.data'.format(test_id=test_id)
        #self.backend = ('http://volta-backend-test.haze.yandex.net:8123', 'volta.logs')
        #self.backend = ('https://lunapark.test.yandex-team.ru/api/volta', 'volta.logs')
        self.backend = ('https://lunapark.yandex-team.ru/api/volta', 'volta.logs')
        self.custom_ts = None

    def setCustomTimestamp(self, ts):
        self.custom_ts = ts

    def FormatEvents(self):
        """
        lists of events data

        Returns:
            list of values w/ lists inside, format: [today, test_id, ts, message]
        """
        logger.info('Started formatting events')
        values = []
        for data in find_event_messages(self.filename):
            date, ts, tag, message = data
            if message.startswith('[volta]'):
                m = re.match(r"\[volta\]\s+(?P<event_ts>\S+)\s+(?P<custom_message>flash_ON.*?)", message)
                if m:
                    custom_data = m.groupdict()
                    event_custom_ts = int(custom_data['event_ts'])/10**9.
                    logger.debug('Event custom ts: %s', event_custom_ts)
                    event_ts = self.sync + (float(event_custom_ts) - float(self.custom_ts))
                    logger.debug('Event synced ts: %s', event_ts)
            else:
                # Android doesn't log `year` to logcat by default
                ts_prepare = datetime.datetime.strptime(
                    "{year}-{date} {time}".format(
                        year=datetime.datetime.now().year,
                        date=date,
                        time=ts
                    ),
                    "%Y-%m-%d %H:%M:%S.%f"
                )
            ts = int(((ts_prepare - datetime.datetime(1970,1,1)).total_seconds()) * 10000 )
            values.append([self.date, self.test_id, ts, "{tag} {message}".format(tag=tag, message=message)])
        return values

    def upload(self):
        with open(self.output_file, 'r') as outfile:
            data = outfile.read()
            url = "{url}{query}".format(
                url="{backend_url}/?query=".format(backend_url=self.backend[0]),
                query="INSERT INTO {backend_table} FORMAT CSV".format(backend_table=self.backend[1])
            )
            headers = {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}
            r = requests.post(url, data=data, headers=headers)
            logger.debug('Upload current to clickhouse status: %s. Message: %s', r.status_code, r.text, exc_info=True)
            r.raise_for_status()
            return


class FileReader(object):
    def __init__(self, fname, slope, offset):
        self.filename = fname
        self.slope = slope
        self.offset = offset

    def binary_to_df(self):
        df = pd.DataFrame(
            np.fromfile(
                self.filename,
                dtype=np.uint16
            ).astype(np.float32) * self.slope + self.offset
        )
        return df[0]

    def plaintext_to_df(self):
        df = pd.read_csv(
            self.filename,
            names=['curr']
        ) * self.slope + self.offset
        return df.curr


def run():
    parser = argparse.ArgumentParser(
        description='upload data to lunapark.')
    parser.add_argument(
        '-f', '--filename',
        help='path to binary file w/ volta output')
    parser.add_argument(
        '-e', '--events',
        help='path to events logcat file')
    parser.add_argument(
        '-s', '--samplerate',
        help='samplerate',
        type=int,
        default=10000
    )
    parser.add_argument(
        '-k', '--slope',
        help='slope, electrical current metric multiplier, `k` in linear y=kx+b function',
        type=float,
        default=float(5000/2**12)
    )
    parser.add_argument(
        '-b', '--offset',
        help='y-offset, electrical current metric addend, `b` in linear y=kx+b function',
        type=float,
        default=0
    )
    parser.add_argument(
        '-m', '--meta',
        help='meta json',
        default=None
    )
    parser.add_argument(
        '-d', '--debug',
        help='enable debug logging',
        action='store_true')
    parser.add_argument(
        '-w', '--binary',
        help='enable binary input log format',
        action='store_true')
    parser.add_argument(
        '-c', '--custom',
        help='enable custom events format',
        action='store_true',
        default=False)
    args = vars(parser.parse_args())
    main(args)


def find_event_messages(log):
    # 12-14 13:32:31.239  3679  3679 I \
    # CameraService: onTorchStatusChangedLocked: Torch status changed for cameraId=0, newStatus=1
    RE = re.compile(r"""
        ^
        (\S+) # date, e.g. 12-14
        \s+
        (\S+) # timestamp, e.g. 13:32:31.239
        \s+
        \S+ # pid, e.g. 3679
        \s+
        \S+ # ppid, e.g. 3679
        \s+
        \S # priority level, e.g. I
        \s+
        (\S+) # tag, e.g. CameraService:
        \s+
        (.*?) # message, e.g. onTorchStatusChangedLocked: Torch status changed for cameraId=0, newStatus=1
        $
    """, re.X)
    with open(log,'r') as eventlog:
        for event in eventlog.readlines():
            match = RE.match(event)
            if match:
                date, ts, tag, message = match.groups(0)
                yield (date, ts, tag, message)


def main(args):
    logging.basicConfig(
        level="DEBUG" if args.get('debug') else "INFO",
        format='%(asctime)s [%(levelname)s] [uploader] %(filename)s:%(lineno)d %(message)s'
    )
    logger.debug('Args: %s', args)
    logger.info('Uploader started.')

    test_id = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")
    date = datetime.datetime.now().strftime("%Y-%m-%d")

    if not args.get('filename'):
        raise ValueError('Unable to run without electrical current measurements file. `-f option`')

    # events log specified, so we trying to find sync point
    if args.get('events'):
        # find sync sample in electrical currents log
        reader = FileReader(args.get('filename'), args.get('slope'), args.get('offset'))
        # volta binary format, uint16
        if args.get('binary'):
            df = reader.binary_to_df()
            logger.debug('Read dataframe: \n%s', df)
        # volta plaintext [old-style 500Hz compatibility]
        else:
            df = reader.plaintext_to_df()
            logger.debug('Read dataframe: \n%s', df)

        # pearson's cross-correlate
        sync_sample = sync(
            df,
            args.get('events'),
            sps=args.get('samplerate'),
            first=args.get('samplerate')*15,
            trailing_zeros=1000,
        )

        # find first flashlight message in events log
        syncflash = None
        for data in find_event_messages(args.get('events')):
            dt, ts, tag, message = data
            if "newStatus=2" in message:
                syncflash = datetime.datetime.strptime(
                    "{year}-{date} {time}".format(
                        year=datetime.datetime.now().year,
                        date=dt,
                        time=ts
                   ),
                    "%Y-%m-%d %H:%M:%S.%f"
                )
                break

        if not syncflash:
            raise ValueError('Unable to find appropriate flashlight messages in android log to synchronize')

        syncflash_unix = (syncflash - datetime.datetime(1970,1,1)).total_seconds()
        sync_point = syncflash_unix - float(sync_sample)/args.get('samplerate')
        logger.info('sync_point found: %s', sync_point)

    # no events log specified, so we take timestamp.now() as syncpoint
    else:
        sync_point = (datetime.datetime.now() - datetime.datetime(1970, 1, 1)).total_seconds()
        logger.info('sync_point is datetime.now(): %s', sync_point)

    if args.get('meta'):
        with open(args.get('meta'), 'r') as jsn_fname:
            data = jsn_fname.read()
            meta = json.loads(data)
    else:
        meta = None

    # create lunapark job
    jobid = CreateJob(test_id, meta, args.get('config'), 'LOAD-272')

    # reformat and upload currents
    current_worker = CurrentsWorker(args, sync_point, date, test_id)
    current_data = current_worker.FormatCurrent()
    WriteListToCSV(current_worker.output_file, current_data)
    current_worker.upload()

    # make and upload events
    if args.get('events'):
        events_worker = EventsWorker(args.get('events'), sync_point, date, test_id)

        # find custom first sync flashlight
        if args.get('custom'):
            for data in find_event_messages(args.get('events')):
                dt, ts, tag, message = data
                if message.startswith('[volta]'):
                    m = re.match(r"\[volta\]\s+(?P<custom_ts>\S+)\s+(?P<custom_message>flash_ON.*?)", message)
                    if m:
                        custom_data = m.groupdict()
                        events_worker.setCustomTimestamp(custom_data['custom_ts'])
                        logger.debug('custom data: %s', custom_data)
                        break

        events_data = events_worker.FormatEvents()
        WriteListToCSV(events_worker.output_file, events_data)
        events_worker.upload()

    logger.info('Lunapark url: %s', jobid)
    return jobid


if __name__ == "__main__":
    run()
