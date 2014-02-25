import urllib
import os
import re
import sys
from zipfile import ZipFile
import time
from datetime import date, datetime
from optparse import make_option

from django.core.management.base import NoArgsCommand
#from django.core.management.base import BaseCommand
from django.db import transaction, connection
from django.conf import settings
from django.utils import timezone
from django.utils.encoding import force_text

from django_sec.models import Company, Index, IndexFile, DATA_DIR

def removeNonAscii(s):
    return "".join(i for i in s if ord(i)<128)

class Command(NoArgsCommand):
    help = "Download new files representing one month of 990s, ignoring months we already have. Each quarter contains hundreds of thousands of filings; will take a while to run. "
    #args = ''
    option_list = NoArgsCommand.option_list + (
        make_option('--start-year',
            default=None),
        make_option('--end-year',
            default=None),
        make_option('--quarter',
            default=None),
        make_option('--delete-prior-indexes',
            action='store_true',
            default=False),
    )
    
    def handle_noargs(self, **options):
        
        start_year = options['start_year']
        if start_year:
            start_year = int(start_year)
        else:
            start_year = date.today().year - 1
            
        end_year = options['end_year']
        if end_year:
            end_year = int(end_year)
        else:
            end_year = date.today().year+1
            
        target_quarter = options['quarter']
        if target_quarter:
            target_quarter = int(target_quarter)
            
        tmp_debug = settings.DEBUG
        settings.DEBUG = False
        transaction.enter_transaction_management()
        transaction.managed(True)
        try:
            for year in range(start_year, end_year):
                for quarter in range(4):
                    if target_quarter and quarter+1 != target_quarter:
                        continue
                    quarter_start_month = quarter*3+1
                    quarter_start_date = date(year, quarter_start_month, 1)
                    if date.today() < quarter_start_date:
                        continue
                    self.get_filing_list(year, quarter+1)
        finally:
            settings.DEBUG = tmp_debug
            transaction.commit()
            transaction.leave_transaction_management()
            connection.close()
                
    def get_filing_list(self, year, quarter):
        """
        Gets the list of filings and download locations for the given year and quarter.
        """
        url='ftp://ftp.sec.gov/edgar/full-index/%d/QTR%d/company.zip' % (year, quarter)
    
        # Download the data and save to a file
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        fn = os.path.join(DATA_DIR, 'company_%d_%d.zip' % (year, quarter))
    
        ifile, _ = IndexFile.objects.get_or_create(year=year, quarter=quarter, defaults=dict(filename=fn))
        if ifile.processed:
            return
        ifile.filename = fn
        
        if not os.path.exists(fn):
            print 'Downloading %s.' % (url,)
            compressed_data = urllib.urlopen(url).read()
            fileout = file(fn,'w')
            fileout.write(compressed_data)
            fileout.close()
            ifile.downloaded = timezone.now()
        
        if not ifile.downloaded:
            ifile.downloaded = timezone.now()
        ifile.save()
        transaction.commit()
        
        # Extract the compressed file
        print 'Opening index file %s.' % (fn,)
        zip = ZipFile(fn)
        zdata = zip.read('company.idx')
        #zdata = removeNonAscii(zdata)
        
        # Parse the fixed-length fields
        bulk_companies = []
        bulk_indexes = []
        bulk_commit_freq = 1000
        status_secs = 3
        lines = zdata.split('\n')
        i = 0
        total = len(lines)
        IndexFile.objects.filter(id=ifile.id).update(total_rows=total)
        last_status = None
        #prior_keys = set(Index.objects.all().values_list('company__cik','date','filename').distinct())#Massive memory consumption
        prior_keys = set()
        print 'Found %i prior index keys.' % len(prior_keys)
        prior_ciks = set(Company.objects.all().values_list('cik', flat=True))
        print 'Found %i prior ciks.' % len(prior_ciks)
        for r in lines[10:]: # Note, first 10 lines are useless headers.
            i += 1
            if ifile.processed_rows and i < ifile.processed_rows:
                continue
            if not last_status or ((datetime.now() - last_status).seconds >= status_secs):
            #if not last_status or not i % 100:
                print '\rProcessing record %i of %i (%.02f%%).' % (i, total, float(i)/total*100),
                sys.stdout.flush()
                last_status = datetime.now()
                IndexFile.objects.filter(id=ifile.id).update(processed_rows=i)
            dt = r[86:98].strip()
            if not dt:
                continue
            dt = date(*map(int, dt.split('-')))
            if r.strip() == '':
                continue
            name = r[0:62].strip()
            
            cik = int(r[74:86].strip())
            if cik not in prior_ciks:
                prior_ciks.add(cik)
                bulk_companies.append(Company(cik=cik, name=force_text(name, errors='replace')))
                
            filename = r[98:].strip()
            key = (cik, dt, filename)#, year, quarter)
            if key in prior_keys:
                continue
            prior_keys.add(key)
            if Index.objects.filter(company__cik=cik, date=dt, filename=filename).exists():
                continue
            bulk_indexes.append(Index(
                company_id=cik,
                form=r[62:74].strip(), # form type
                date=dt, # date filed
                year=year,
                quarter=quarter,
                filename=filename,
            ))
            if not len(bulk_indexes) % bulk_commit_freq:
                if len(bulk_companies):
                    Company.objects.bulk_create(bulk_companies)
                    bulk_companies = []
                Index.objects.bulk_create(bulk_indexes)
                bulk_indexes = []
                transaction.commit()
                
        if bulk_indexes:
            if len(bulk_companies):
                Company.objects.bulk_create(bulk_companies)
                bulk_companies = []
            Index.objects.bulk_create(bulk_indexes)
        IndexFile.objects.filter(id=ifile.id).update(processed=timezone.now())
        transaction.commit()
        
        print '\rProcessing record %i of %i (%.02f%%).' % (total, total, 100),
        sys.stdout.flush()
        IndexFile.objects.filter(id=ifile.id).update(processed_rows=total)
        