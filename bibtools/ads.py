# -*- mode: python; coding: utf-8 -*-
# Copyright 2014 Peter Williams <peter@newton.cx>
# Licensed under the GNU General Public License, version 3 or higher.

"""
Tools relating to working with NASA's ADS.
"""

from __future__ import absolute_import, division, print_function, unicode_literals
import json

from .util import *
from . import webutil as wu
from .bibcore import *

__all__ = ('autolearn_bibcode search_ads').split ()


def _translate_ads_name (name):
    pieces = [x.strip () for x in name.split (',', 1)]
    surname = pieces[0].replace (' ', '_')
    # TODO use bibcore

    if len (pieces) > 1:
        return pieces[1] + ' ' + surname
    return surname


def _autolearn_bibcode_tag (info, tag, text):
    # TODO: editors?

    if tag == 'T':
        info['title'] = text
    elif tag == 'D':
        info['year'] = int (text.split ('/')[-1])
    elif tag == 'B':
        info['abstract'] = text
    elif tag == 'A':
        info['authors'] = [_translate_ads_name (n) for n in text.split (';')]
    elif tag == 'Y':
        subdata = dict (s.strip ().split (': ', 1)
                        for s in text.split (';'))

        if 'DOI' in subdata:
            info['doi'] = subdata['DOI']
        if 'eprintid' in subdata:
            value = subdata['eprintid']
            if value.startswith ('arXiv:'):
                info['arxiv'] = value[6:]


def autolearn_bibcode (app, bibcode):
    # XXX could/should convert this to an ADS 2.0 record search, something
    # like http://adslabs.org/adsabs/api/record/{doi}/?dev_key=...

    url = ('http://adsabs.harvard.edu/cgi-bin/nph-abs_connect?'
           'data_type=PORTABLE&nocookieset=1&bibcode=' + wu.urlquote (bibcode))

    info = {'bibcode': bibcode, 'keep': 0} # because we're autolearning
    curtag = curtext = None

    print ('[Parsing', url, '...]')

    for line in wu.urlopen (url):
        line = line.decode ('iso-8859-1').strip ()

        if not len (line):
            if curtag is not None:
                _autolearn_bibcode_tag (info, curtag, curtext)
                curtag = curtext = None
            continue

        if curtag is None:
            if line[0] == '%':
                # starting a new tag
                curtag = line[1]
                curtext = line[3:]
            elif line.startswith ('Retrieved '):
                if not line.endswith ('selected: 1.'):
                    die ('matched more than one publication')
        else:
            if line[0] == '%':
                # starting a new tag, while we had one going before.
                # finish up the previous
                _autolearn_bibcode_tag (info, curtag, curtext)
                curtag = line[1]
                curtext = line[3:]
            else:
                curtext += ' ' + line

    if curtag is not None:
        _autolearn_bibcode_tag (info, curtag, curtext)

    return info


# Searching

def _run_ads_search (app, searchterms, filterterms):
    # TODO: access to more API args
    apikey = app.cfg.get_or_die ('api-keys', 'ads')

    q = [('q', ' '.join (searchterms)),
         ('dev_key', apikey)]

    for ft in filterterms:
        q.append (('filter', ft))

    url = 'http://adslabs.org/adsabs/api/search/?' + wu.urlencode (q)
    return json.load (wu.urlopen (url))


def search_ads (app, terms, raw=False):
    if len (terms) < 2:
        die ('require at least two search terms for ADS')

    adsterms = []

    for info in terms:
        if info[0] == 'year':
            adsterms.append ('year:%d' % info[1])
        elif info[0] == 'surname':
            adsterms.append ('author:"%s"' % info[1])
        else:
            die ('don\'t know how to express search term %r to ADS', info)

    try:
        r = _run_ads_search (app, adsterms, ['database:astronomy']) # XXX more hardcoding
    except Exception as e:
        die ('could not perform ADS search: %s', e)

    if raw:
        json.dump (r, sys.stdout, ensure_ascii=False, indent=2, separators=(',', ': '))
        return

    maxbclen = 0
    info = []

    for item in r['results']['docs'][:20]:
        # year isn't important since it's embedded in bibcode.
        if 'title' in item:
            title = item['title'][0] # not sure why this is a list?
        else:
            title = '(no title)' # this happens, e.g.: 1991PhDT.......161G
        bibcode = item['bibcode']
        authors = ', '.join (parse_name (_translate_ads_name (n))[1] for n in item['author'])

        maxbclen = max (maxbclen, len (bibcode))
        info.append ((bibcode, title, authors))

    ofs = maxbclen + 2
    red, reset = get_color_codes (None, 'red', 'reset')

    for bc, title, authors in info:
        print ('%s%*s%s  ' % (red, maxbclen, bc, reset), end='')
        print_truncated (title, ofs, color='bold')
        print ('    ', end='')
        print_truncated (authors, 4)
