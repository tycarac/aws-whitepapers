from datetime import datetime
from dateutil.parser import *
import dateutil
import logging
import json
from pathlib import Path
import pytz
import re
import time
from urllib import parse
from urllib3 import exceptions

from common import Record, Changed, Result, url_client, local_tz
from paths import sanitize_filename

logger = logging.getLogger(__name__)

category_re = re.compile(r'(?:<a\s[^>]*>)([^<]*)</a>', re.IGNORECASE)
desc_re = re.compile(r'(?:</?p>)?([^<]+)<p>', re.IGNORECASE)


# _____________________________________________________________________________
class FetchItemList(object):

    # _____________________________________________________________________________
    def __init__(self, config_settings, paths):
        self._paths = paths

        remote_settings = config_settings['remote']
        self._source_url = remote_settings['urlLoc']
        self._source_parameters = remote_settings['urlParameters']

        list_cache_settings = config_settings['cache']
        self._cache_age_sec = int(list_cache_settings.get('age', 300))

    # _____________________________________________________________________________
    def process_list(self, list_pages):
        def build_filename(fname, fdate, url):
            path = parse.urlparse(url).path
            loc = path.rfind('.')
            fname = '%s - (%s)' % (sanitize_filename(fname), fdate.strftime('%Y-%m'))
            return (fname + path[loc:]) if loc >= 0 else fname

        logger.debug('process list')
        records = []
        for page in list_pages:
            for grp in page['items']:
                item = grp['item']
                adfields = item['additionalFields']
                name = item['name']
                title = adfields['docTitle']
                category = m.group(1).lower() if (m := category_re.search(adfields['description'])) else None
                content_type = adfields['contentType']

                # Extract text up to HTML tag from "description" and normalize whitespacing
                desc = m.group(1) if (m := desc_re.search(adfields['description'])) else None
                desc = ' '.join(desc.split())

                # Derive date from datetime and not from JSON data file
                date_time_created = dateutil.parser.parse(item['dateCreated'])
                date_time_updated = dateutil.parser.parse(item['dateUpdated'])
                date_published = datetime.date(dateutil.parser.parse(adfields['datePublished']))
                date_sort = datetime.date(dateutil.parser.parse(adfields['sortDate']))
                date_created = datetime.date(date_time_created)
                date_updated = datetime.date(date_time_updated)

                # Extract paths
                url = adfields['primaryURL'].split('?')[0]
                # filename = build_filename(title, date_published, url)
                filename = build_filename(title, date_sort, url)
                rel_filepath = Path(content_type, filename) if category else None

                records.append(Record(name, title, category, content_type, desc,
                    date_created, date_updated, date_published, date_sort, date_time_created, date_time_updated,
                    url, filename, rel_filepath, Changed.nil, Result.nil))

        logger.info('Number items: %d' % len(records))
        return records

    # _____________________________________________________________________________
    def fetch_list_page(self, page_num, fields):
        logger.info('> %4d fetching list page %3d' % (page_num, page_num))
        list_page, cache_pf = None, None
        hits_total, count = 0, 0

        try:
            rsp = url_client.request('GET', self._source_url, fields=fields)
            logger.debug('> %4d response status  %d' % (page_num, rsp.status))
            if rsp.status == 200:
                # extract data
                list_page = json.loads(rsp.data.decode('utf-8'))
                metadata = list_page['metadata']
                count = int(metadata['count'])
                hits_total = int(metadata['totalHits'])

                # Write list page to cache
                if count > 1:
                    cache_pf = Path(self._paths['cachePath'], '%s.%03d.json' %
                                (self._paths['name'], page_num)).resolve()
                    logger.debug('> %4d write %s' % (page_num, cache_pf.name))
                    cache_pf.write_text(json.dumps(list_page, indent=2))
        except exceptions.SSLError as ex:
            logger.exception('> %4d SSLError' % page_num)
        except exceptions.HTTPError as ex:
            logger.exception('> %4d HTTPError' % page_num)

        return list_page, count, hits_total, cache_pf

    # _____________________________________________________________________________
    def fetch_list(self):
        logger.debug('fetch list')
        logger.info('URL: %s' % self._source_url)

        list_pages, cache_files = [], []
        hits_count, page_num = 0, 0
        fields = self._source_parameters.copy()
        while True:
            list_page, count, hits_total, cache_pf = self.fetch_list_page(page_num, fields)
            logger.debug('> %4d hits total, hits count, count: %d, %d, %d' % (page_num, hits_total, hits_count, count))
            if count < 1:
                break
            list_pages.append(list_page)
            hits_count += count
            cache_files.append(cache_pf)
            if hits_count >= hits_total:
                break
            page_num += 1
            fields['page'] = page_num

        # Write summary file
        now_dt = datetime.utcnow()
        summary = r'{"written":{"utc":"%s","local":"%s"},"count":"%d","pages":"%d"}' % (pytz.UTC.localize(now_dt),
                    local_tz.localize(now_dt), hits_count, len(cache_files))
        summary_filepath = self._paths['summaryFilepath']
        summary_filepath.write_text(json.dumps(json.loads(summary), indent=2))
        cache_files.append(summary_filepath)

        # Remove superfluous cache files
        cache_path = self._paths['cachePath']
        [p.unlink() for p in cache_path.glob('*.*') if p not in cache_files]

        return list_pages

    # _____________________________________________________________________________
    def build_list(self):
        logger.debug('build list')
        logger.debug('Cache path: %s' % self._paths['cachePath'])

        # Test local cached age
        is_use_cache = False
        summary_filepath = self._paths['summaryFilepath']
        if self._cache_age_sec > 0 and summary_filepath.exists():
            is_use_cache = summary_filepath.stat().st_mtime > (time.time() - self._cache_age_sec)

        # Build list
        list_pages = []
        cache_path = self._paths['cachePath']
        logger.info('Use cached list: %s' % is_use_cache)
        if is_use_cache:
            [list_pages.append(json.loads(p.read_text())) for p in cache_path.glob('*.*') if
                not p.samefile(summary_filepath)]
        else:
            cache_path.mkdir(parents=True, exist_ok=True)
            list_pages = self.fetch_list()
        records = self.process_list(list_pages)

        return records
