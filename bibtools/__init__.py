# -*- mode: python; coding: utf-8 -*-
# Copyright 2014 Peter Williams <peter@newton.cx>
# Licensed under the GNU General Public License, version 3 or higher.

"""
Docstring!
"""

import codecs, collections, cookielib, errno, json, os.path, re, sqlite3, sys, urllib2
import HTMLParser # renamed to html.parser in Python 3.

from .util import *
from .config import BibConfig


class BibError (Exception):
    def __init__ (self, fmt, *args):
        if not len (args):
            self.bibmsg = str (fmt)
        else:
            self.bibmsg = fmt % args

    def __str__ (self):
        return self.bibmsg


class UsageError (BibError):
    pass

class PubLocateError (BibError):
    pass

class MultiplePubsError (PubLocateError):
    pass

dbpath = bibpath ('db.sqlite3')




# Monkeying with names!
#
# We store names like so:
#   "Albert J. von_Trapp_Rodolfo,_Jr."
# As far as I can see, this makes it easy to pull out surnames and
# deal with all that mess. We're pretty boned once I start dealing with
# papers whose have author names given in both Latin and Chinese characters,
# though.
#
# Another thing to be wary of is "names" like "The Fermi-LAT Collaboration".
# Some Indians have only single names (e.g. "Gopal-Krishna").
#
# NFAS = normalized first-author surname. We decapitalize, remove accents,
# and replace nonletters with periods, so it's a gmail-ish form.

def parse_name (text):
    first, last = text.rsplit (' ', 1)
    return first, last.replace ('_', ' ')


def encode_name (given, family):
    return given + ' ' + family.replace (' ', '_')


def _translate_ads_name (name):
    pieces = [x.strip () for x in name.split (',', 1)]
    surname = pieces[0].replace (' ', '_')

    if len (pieces) > 1:
        return pieces[1] + ' ' + surname
    return surname


def _translate_unixref_name (personelem):
    # XXX: deal with "The Fermi-LAT Collaboration", "Gopal-Krishna", etc.

    given = personelem.find ('given_name').text
    sur = personelem.find ('surname').text
    return given + ' ' + sur.replace (' ', '_')


def _translate_arxiv_name (auth):
    # XXX I assume that we don't get standardized names out of Arxiv, so
    # nontrivial last names will be gotten wrong. I don't see any point
    # in trying to solve this here.
    return auth.find (_atom_ns + 'name').text


def _translate_bibtex_name (name):
    # generically: von Foo, Jr, Bob C. ; citeulike always gives us comma form
    a = [i.strip () for i in name.split (',')]

    if len (a) == 0:
        warn ('all last name, I think: %s', name)
        return a.replace (' ', '_')

    first = a[-1]
    surname = (',_'.join (a[:-1])).replace (' ', '_')

    if not len (first):
        warn ('CiteULike mis-parsed name, I think: %s', name)

    return first + ' ' + surname


def normalize_surname (name):
    from unicodedata import normalize
    # this strips accents:
    name = normalize ('NFKD', unicode (name)).encode ('ascii', 'ignore')
    # now strip non-letters and condense everything:
    return re.sub (r'\.\.+', '.', re.sub (r'[^a-z]+', '.', name.lower ()))


# The database!

def connect ():
    return sqlite3.connect (dbpath, factory=BibDB)


PubRow = collections.namedtuple ('PubRow',
                                 'id abstract arxiv bibcode doi keep nfas '
                                 'refdata title year'.split ())

AUTHTYPE_AUTHOR = 0
AUTHTYPE_EDITOR = 1

AuthorNameRow = collections.namedtuple ('AuthorNameRow',
                                        ['name'])

AuthorRow = collections.namedtuple ('AuthorRow',
                                    'type pubid idx authid'.split ())

HistoryRow = collections.namedtuple ('HistoryRow',
                                     'date pubid action'.split ())

NicknameRow = collections.namedtuple ('NicknameRow',
                                      'nickname pubid'.split ())

PdfRow = collections.namedtuple ('PdfRow',
                                 'sha1 pubid'.split ())


HA_READ = 1 # history actions
HA_VISIT = 2


def nt_augment (ntclass, **vals):
    for k in vals.iterkeys ():
        if k not in ntclass._fields:
            raise ValueError ('illegal field "%s" for creating %s instance'
                              % (k, ntclass.__name__))
    return ntclass (*tuple (vals.get (k) for k in ntclass._fields))


class BibDB (sqlite3.Connection):
    def getfirst (self, fmt, *args):
        """Returns the tuple from sqlite3, or None."""
        return self.execute (fmt, args).fetchone ()


    def getfirstval (self, fmt, *args):
        """Assumes that the query returns a single column. Returns the first value, or
        None."""
        v = self.getfirst (fmt, *args)
        if v is None:
            return None
        return v[0]


    def locate_pubs (self, textids, noneok=False, autolearn=False):
        for textid in textids:
            kind, text = classify_pub_ref (textid)

            c = self.cursor ()
            c.row_factory = lambda curs, tup: PubRow (*tup)

            q = matchtext = None

            if kind == 'doi':
                q = c.execute ('SELECT * FROM pubs WHERE doi = ?', (text, ))
                matchtext = 'DOI = ' + text
            elif kind == 'bibcode':
                q = c.execute ('SELECT * FROM pubs WHERE bibcode = ?', (text, ))
                matchtext = 'bibcode = ' + text
            elif kind == 'arxiv':
                q = c.execute ('SELECT * FROM pubs WHERE arxiv = ?', (text, ))
                matchtext = 'arxiv = ' + text
            elif kind == 'nickname':
                q = c.execute ('SELECT p.* FROM pubs AS p, nicknames AS n '
                               'WHERE p.id == n.pubid AND n.nickname = ?', (text, ))
                matchtext = 'nickname = ' + text
            elif kind == 'nfasy':
                nfas, year = text.rsplit ('.', 1)
                if year == '*':
                    q = c.execute ('SELECT * FROM pubs WHERE nfas = ?', (nfas, ))
                else:
                    q = c.execute ('SELECT * FROM pubs WHERE nfas = ? '
                                   'AND year = ?', (nfas, year))
                matchtext = 'surname/year ~ ' + text
            else:
                # This is a bug since we should handle every possible 'kind'
                # returned by classify_pub_ref.
                assert False

            gotany = False

            for pub in q:
                gotany = True
                yield pub

            if not gotany and autolearn:
                yield self.learn_pub (autolearn_pub (textid))
                continue

            if not gotany and not noneok:
                raise PubLocateError ('no publications matched ' + textid)


    def locate_pub (self, text, noneok=False, autolearn=False):
        if autolearn:
            noneok = True

        thepub = None

        for pub in self.locate_pubs ((text,), noneok, autolearn):
            if thepub is None:
                # First match.
                thepub = pub
            else:
                # Second match. There will be no third match.
                raise MultiplePubsError ('more than one publication matched ' + text)

        if thepub is not None:
            return thepub

        if autolearn:
            return self.learn_pub (autolearn_pub (text))

        # If we made it here, noneok must be true.
        return None


    def locate_or_die (self, text, autolearn=False):
        try:
            return self.locate_pub (text, autolearn=autolearn)
        except MultiplePubsError as e:
            print >>sys.stderr, 'error:', e
            print >>sys.stderr
            print_generic_listing (self, self.locate_pubs ((text,), noneok=True))
            raise SystemExit (1)
        except PubLocateError as e:
            die (e)


    def pub_fquery (self, q, *args):
        c = self.cursor ()
        c.row_factory = lambda curs, tup: PubRow (*tup)
        return c.execute (q, args)


    def pub_query (self, partial, *args):
        return self.pub_fquery ('SELECT * FROM pubs WHERE ' + partial, *args)


    def try_get_pdf_for_id (self, proxy, id):
        r = self.getfirst ('SELECT arxiv, bibcode, doi FROM pubs WHERE id = ?', id)
        arxiv, bibcode, doi = r

        mkdir_p (bibpath ('lib'))
        temppath = bibpath ('lib', 'incoming.pdf')

        sha1 = try_fetch_pdf (proxy, temppath,
                              arxiv=arxiv, bibcode=bibcode, doi=doi)
        if sha1 is None:
            return None

        ensure_libpath_exists (sha1)
        destpath = libpath (sha1, 'pdf')
        os.rename (temppath, destpath)
        self.execute ('INSERT OR REPLACE INTO pdfs VALUES (?, ?)', (sha1, id))
        return sha1


    def learn_pub_authors (self, pubid, authtype, authors):
        c = self.cursor ()

        for idx, auth in enumerate (authors):
            # Based on reading StackExchange, there's no cleaner way to do this,
            # but the SELECT should be snappy.
            c.execute ('INSERT OR IGNORE INTO author_names VALUES (?)',
                       (auth, ))
            row = self.getfirst ('SELECT oid FROM author_names WHERE name = ?', auth)[0]
            c.execute ('INSERT OR REPLACE INTO authors VALUES (?, ?, ?, ?)',
                       (authtype, pubid, idx, row))


    def get_pub_authors (self, pubid, authtype=AUTHTYPE_AUTHOR):
        return (parse_name (a[0]) for a in
                self.execute ('SELECT name FROM authors AS au, author_names AS an '
                              'WHERE au.type == ? AND au.authid == an.oid '
                              '  AND au.pubid == ? '
                              'ORDER BY idx', (authtype, pubid, )))


    def get_pub_fas (self, pubid):
        """FAS = first-author surname. May return None. We specifically are retrieving
        the un-normalized version here, so we don't use the value stored in
        the 'pubs' table."""

        for t in self.execute ('SELECT name FROM authors AS au, author_names AS an '
                               'WHERE au.type == ? AND au.authid == an.oid '
                               '  AND au.pubid == ? '
                               'AND idx == 0', (AUTHTYPE_AUTHOR, pubid, )):
            return parse_name (t[0])[1]

        return None


    def choose_pub_nickname (self, pubid):
        # barring any particularly meaningful information, go with the
        # shortest nickname. Returns None if none present.

        n = list (self.execute ('SELECT nickname FROM nicknames '
                                'WHERE pubid == ? '
                                'ORDER BY length(nickname) ASC LIMIT 1', (pubid, )))

        if not len (n):
            return None
        return n[0][0]


    def _lint_refdata (self, info):
        rd = info['refdata']

        if rd.get ('journal') == 'ArXiv e-prints':
            warn ('useless "ArXiv e-prints" bibliographical record')


    def _fill_pub (self, info, pubid):
        """Note that `info` will be mutated.

        If pubid is None, a new record will be created; otherwise it will
        be updated."""

        authors = info.pop ('authors', ())
        editors = info.pop ('editors', ())
        nicknames = info.pop ('nicknames', ())

        if 'abstract' in info:
            info['abstract'] = squish_spaces (info['abstract'])
        if 'title' in info:
            info['title'] = squish_spaces (info['title'])

        if authors:
            info['nfas'] = normalize_surname (parse_name (authors[0])[1])

        if 'refdata' in info:
            self._lint_refdata (info)
            info['refdata'] = json.dumps (info['refdata'])

        row = nt_augment (PubRow, **info)
        c = self.cursor ()

        if pubid is not None:
            # not elegant but as far as I can tell there's no alternative.
            c.execute ('UPDATE pubs SET abstract=?, arxiv=?, bibcode=?, '
                       '  doi=?, keep=?, nfas=?, refdata=?, title=?, year=? '
                       'WHERE id == ?', row[1:] + (pubid, ))
        else:
            c.execute ('INSERT INTO pubs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', row)
            pubid = c.lastrowid

        if authors:
            self.learn_pub_authors (pubid, AUTHTYPE_AUTHOR, authors)

        if editors:
            self.learn_pub_authors (pubid, AUTHTYPE_EDITOR, editors)

        if nicknames:
            for nickname in nicknames:
                try:
                    c.execute ('INSERT INTO nicknames VALUES (?, ?)',
                               (nickname, pubid))
                except sqlite3.IntegrityError:
                    die ('duplicated pub nickname "%s"', nickname)

        tmp = list (row)
        tmp[0] = pubid
        return PubRow (*tmp)


    def learn_pub (self, info):
        """Note that `info` will be mutated."""
        return self._fill_pub (info, None)


    def update_pub (self, pub, info):
        info['keep'] = pub.keep

        self.execute ('DELETE FROM authors WHERE pubid == ?', (pub.id, ))
        self.execute ('DELETE FROM nicknames WHERE pubid == ?', (pub.id, ))
        # XXX later maybe:
        #self.execute ('DELETE FROM notes WHERE pubid == ?', (pub.id, ))
        #self.execute ('DELETE FROM publists WHERE pubid == ?', (pub.id, ))

        return self._fill_pub (info, pub.id)


    def delete_pub (self, pubid):
        sha1 = self.getfirstval ('SELECT sha1 FROM pdfs WHERE pubid == ?', pubid)
        if sha1 is not None:
            warn ('orphaning file %s', libpath (sha1, 'pdf'))

        self.execute ('DELETE FROM authors WHERE pubid == ?', (pubid, ))
        self.execute ('DELETE FROM history WHERE pubid == ?', (pubid, ))
        self.execute ('DELETE FROM nicknames WHERE pubid == ?', (pubid, ))
        self.execute ('DELETE FROM notes WHERE pubid == ?', (pubid, ))
        self.execute ('DELETE FROM pdfs WHERE pubid == ?', (pubid, ))
        self.execute ('DELETE FROM publists WHERE pubid == ?', (pubid, ))
        self.execute ('DELETE FROM pubs WHERE id == ?', (pubid, ))

        # at some point the author_names table will need rebuilding, but
        # I don't think we should worry about that here.


    def log_action (self, pubid, actionid):
        import time
        self.execute ('INSERT INTO history VALUES (?, ?, ?)',
                      (int (time.time ()), pubid, actionid))


# Bibliography logic

_arxiv_re_1 = re.compile (r'^\d\d[01]\d\.\d+')
_arxiv_re_2 = re.compile (r'^[a-z-]+/\d+')
_bibcode_re = re.compile (r'^\d\d\d\d[a-zA-Z0-9&]+')
_doi_re = re.compile (r'^10\.\d+/.*')
_fasy_re = re.compile (r'.*\.(\d+|\*)$')

def classify_pub_ref (text):
    """Given some text that we believe identifies a publication, try to
    figure out how it does so."""

    if text.startswith ('doi:'):
        return 'doi', text[4:]

    if _doi_re.match (text) is not None:
        return 'doi', text

    if _bibcode_re.match (text) is not None:
        return 'bibcode', text

    if _arxiv_re_1.match (text) is not None:
        return 'arxiv', text

    if _arxiv_re_2.match (text) is not None:
        return 'arxiv', text

    if text.startswith ('arxiv:'):
        return 'arxiv', text[6:]

    if _fasy_re.match (text) is not None:
        # This test should go very low since it's quite open-ended.
        surname, year = text.rsplit ('.', 1)
        return 'nfasy', normalize_surname (surname) + '.' + year

    return 'nickname', text


def try_fetch_pdf (proxy, destpath, arxiv=None, bibcode=None, doi=None):
    """Given reference information, download a PDF to a specified path. Returns
    the SHA1 sum of the PDF as a hexadecimal string, or None if we couldn't
    figure out how to download it."""

    pdfurl = None

    if doi is not None:
        jurl = doi_to_journal_url (doi)
        print '[Attempting to scrape', jurl, '...]'
        pdfurl = proxy.unmangle (scrape_pdf_url (proxy.open (jurl)))

    if pdfurl is None and bibcode is not None:
        # This never returns None: ADS will always give a URL, but it may just
        # be that the URL resolves to a 404 page saying that ADS has no PDF
        # available. Thus, this technique is always our last resort.
        pdfurl = bibcode_to_maybe_pdf_url (bibcode)

    if pdfurl is None and arxiv is not None:
        # Always prefer non-preprints. I need to straighten out how I'm going
        # to deal with them ...
        pdfurl = 'http://arxiv.org/pdf/' + urlquote (arxiv) + '.pdf'

    if pdfurl is None:
        return None

    # OK, we can now download and register the PDF. TODO: progress reporting,
    # etc.

    import hashlib
    s = hashlib.sha1 ()

    print '[Trying', pdfurl, '...]'

    try:
        resp = proxy.open (pdfurl)
    except urllib2.HTTPError as e:
        from urlparse import urlparse
        if e.code == 404 and urlparse (pdfurl)[1] == 'articles.adsabs.harvard.edu':
            warn ('ADS doesn\'t actually have the PDF on file')
            return None # ADS gave us a URL that turned out to be a lie.
        raise

    first = True

    with open (destpath, 'w') as f:
        while True:
            b = resp.read (4096)

            if first:
                if len (b) < 4 or b[:4] != '%PDF':
                    warn ('response does not seem to be a PDF')
                    resp.close ()
                    f.close ()
                    os.unlink (temppath)
                    return None
                first = False

            if not len (b):
                break

            s.update (b)
            f.write (b)

    return s.hexdigest ()


# Web scraping, proxy, etc. helpers.

class NonRedirectingProcessor (urllib2.HTTPErrorProcessor):
    # Copied from StackOverflow q 554446.
    def http_response (self, request, response):
        return response

    https_response = http_response


def get_url_from_redirection (url):
    """Note that we don't go through the proxy class here for convenience, under
    the assumption that all of these redirections involve public information
    that won't require privileged access."""

    opener = urllib2.build_opener (NonRedirectingProcessor ())
    resp = opener.open (url)

    if resp.code not in (301, 302, 303, 307) or 'Location' not in resp.headers:
        die ('expected a redirection response for URL %s but didn\'t get one', url)

    resp.close ()
    return resp.headers['Location']


class HarvardProxyLoginParser (HTMLParser.HTMLParser):
    def __init__ (self):
        HTMLParser.HTMLParser.__init__ (self)
        self.formurl = None
        self.inputs = []


    def handle_starttag (self, tag, attrs):
        if tag == 'form':
            attrs = dict (attrs)
            self.formurl = attrs.get ('action')
            if attrs.get ('method') != 'post':
                die ('unexpected form method')
        elif tag == 'input':
            attrs = dict (attrs)
            if 'name' not in attrs or 'value' not in attrs:
                die ('missing form input information')
            self.inputs.append ((attrs['name'], attrs['value']))


def parse_http_html (resp, parser):
    debug = False

    charset = resp.headers.getparam ('charset')
    if charset is None:
        charset = 'ISO-8859-1'

    dec = codecs.getincrementaldecoder (charset) ()

    if debug:
        f = open ('debug.html', 'w')

    while True:
        d = resp.read (4096)
        if not len (d):
            text = dec.decode ('', final=True)
            parser.feed (text)
            break

        if debug:
            f.write (d)

        text = dec.decode (d)
        parser.feed (text)

    if debug:
        f.close ()

    resp.close ()
    parser.close ()
    return parser


class HarvardProxy (object):
    suffix = '.ezp-prod1.hul.harvard.edu'
    loginurl = 'https://www.pin1.harvard.edu/cas/login'
    forwardurl = 'http://ezp-prod1.hul.harvard.edu/connect'

    default_inputs = [
        ('compositeAuthenticationSourceType', 'PIN'),
    ]

    def __init__ (self, username, password):
        self.cj = cookielib.CookieJar ()
        self.opener = urllib2.build_opener (urllib2.HTTPRedirectHandler (),
                                            urllib2.HTTPCookieProcessor (self.cj))

        # XXX This doesn't quite belong here. We need it because otherwise
        # nature.com gives us the mobile site, which happens to not include
        # the easily-recognized <a> tag linking to the paper PDF. I don't know
        # exactly what's needed, but if we just send 'Mozilla/5.0' as the UA,
        # nature.com gives us a 500 error (!). So I've just copied my current
        # browser's UA.
        ua = BibConfig ().get_or_die ('proxy', 'user-agent')
        self.opener.addheaders = [('User-Agent', ua)]

        self.inputs = list (self.default_inputs)
        self.inputs.append (('username', username))
        self.inputs.append (('password', password))


    def login (self, resp):
        # XXX we should verify the SSL cert of the counterparty, lest we send
        # our password to malicious people.
        parser = parse_http_html (resp, HarvardProxyLoginParser ())

        if parser.formurl is None:
            die ('malformed proxy page response?')

        from urlparse import urljoin
        posturl = urljoin (resp.url, parser.formurl)
        values = {}

        for name, value in parser.inputs:
            values[name] = value

        for name, value in self.inputs:
            values[name] = value

        from urllib import urlencode # yay terrible Python APIs
        req = urllib2.Request (posturl, urlencode (values))
        # The response will redirect to the original target page.
        return self.opener.open (req)


    def open (self, url):
        from urlparse import urlparse, urlunparse
        scheme, loc, path, params, query, frag = urlparse (url)

        if loc.endswith ('arxiv.org'):
            # For whatever reason, the proxy server doesn't work
            # if we try to access Arxiv with it.
            proxyurl = url
        else:
            loc += self.suffix
            proxyurl = urlunparse ((scheme, loc, path, params, query, frag))

        resp = self.opener.open (proxyurl)

        if resp.url.startswith (self.loginurl):
            resp = self.login (resp)

        if resp.url.startswith (self.forwardurl):
            # Sometimes we get forwarded to a separate cookie-setting page
            # that requires us to re-request the original URL.
            resp = self.opener.open (proxyurl)

        return resp


    def unmangle (self, url):
        if url is None:
            return None # convenience

        from urlparse import urlparse, urlunparse

        scheme, loc, path, params, query, frag = urlparse (url)
        if not loc.endswith (self.suffix):
            return url

        loc = loc[:-len (self.suffix)]
        return urlunparse ((scheme, loc, path, params, query, frag))


class PDFUrlScraper (HTMLParser.HTMLParser):
    """Observed places to look for PDF URLs:

    <meta> tag with name=citation_pdf_url -- IOP
    <a> tag with id=download-pdf -- Nature (non-mobile site, newer)
    <a> tag with class=download-pdf -- Nature (older)
    <a> tag with class=pdf -- AIP
    """

    def __init__ (self):
        HTMLParser.HTMLParser.__init__ (self)
        self.pdfurl = None


    def handle_starttag (self, tag, attrs):
        if self.pdfurl is not None:
            return

        if tag == 'meta':
            attrs = dict (attrs)
            if attrs.get ('name') == 'citation_pdf_url':
                self.pdfurl = attrs['content']
        elif tag == 'a':
            attrs = dict (attrs)
            if attrs.get ('id') == 'download-pdf':
                self.pdfurl = attrs['href']
            elif attrs.get ('class') == 'download-pdf':
                self.pdfurl = attrs['href']
            elif attrs.get ('class') == 'pdf':
                self.pdfurl = attrs['href']


def scrape_pdf_url (resp):
    parser = parse_http_html (resp, PDFUrlScraper ())
    if parser.pdfurl is None:
        return None

    from urlparse import urljoin
    return urljoin (resp.url, parser.pdfurl)


def doi_to_journal_url (doi):
    return get_url_from_redirection ('http://dx.doi.org/' + urlquote (doi))


def bibcode_to_maybe_pdf_url (bibcode):
    """If ADS doesn't have a fulltext link for a given bibcode, it will return a link
    to articles.ads.harvard.edu that in turn yields an HTML error page.

    Also, the Location header returned by the ADS server appears to be slightly broken,
    with the &'s in the URL being HTML entity-encoded to &amp;s."""

    url = ('http://adsabs.harvard.edu/cgi-bin/nph-data_query?link_type=ARTICLE&bibcode='
           + urlquote (bibcode))
    pdfurl = get_url_from_redirection (url)
    return pdfurl.replace ('&amp;', '&')


def doi_to_maybe_bibcode (doi):
    bibcode = None

    # XXX could convert this to an ADS 2.0 record search, something like
    # http://adslabs.org/adsabs/api/record/{doi}/?dev_key=...

    url = ('http://adsabs.harvard.edu/cgi-bin/nph-abs_connect?'
           'data_type=Custom&format=%25R&nocookieset=1&doi=' +
           urlquote (doi))
    lastnonempty = None

    for line in urllib2.urlopen (url):
        line = line.strip ()
        if len (line):
            lastnonempty = line

    if lastnonempty is None:
        return None
    if lastnonempty.startswith ('Retrieved 0 abstracts'):
        return None

    return lastnonempty


# Autolearning publications

def autolearn_pub (text):
    kind, text = classify_pub_ref (text)

    if kind == 'doi':
        # ADS seems to have better data quality.
        bc = doi_to_maybe_bibcode (text)
        if bc is not None:
            print '[Associated', text, 'to', bc + ']'
            kind, text = 'bibcode', bc

    if kind == 'doi':
        return autolearn_doi (text)

    if kind == 'bibcode':
        return autolearn_bibcode (text)

    if kind == 'arxiv':
        return autolearn_arxiv (text)

    die ('cannot auto-learn publication "%s"', text)


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


def autolearn_bibcode (bibcode):
    # XXX could/should convert this to an ADS 2.0 record search, something
    # like http://adslabs.org/adsabs/api/record/{doi}/?dev_key=...

    url = ('http://adsabs.harvard.edu/cgi-bin/nph-abs_connect?'
           'data_type=PORTABLE&nocookieset=1&bibcode=' + urlquote (bibcode))

    info = {'bibcode': bibcode, 'keep': 0} # because we're autolearning
    curtag = curtext = None

    print '[Parsing', url, '...]'

    for line in urllib2.urlopen (url):
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


def autolearn_doi (doi):
    # TODO: editors. See e.g. unixref output for 10.1007/978-3-642-14335-9_1
    # -- three <contributors> sections (!), with contributor_role="editor" on
    # the <person_name> element.

    import xml.etree.ElementTree as ET

    # XXX loading config scattershot as-needed isn't ideal ...
    apikey = BibConfig ().get_or_die ('api-keys', 'crossref')

    url = ('http://crossref.org/openurl/?id=%s&noredirect=true&pid=%s&'
           'format=unixref' % (urlquote (doi), urlquote (apikey)))
    info = {'doi': doi, 'keep': 0} # because we're autolearning

    # XXX sad to be not doing this incrementally, but Py 2.x doesn't
    # seem to have an incremental parser built in.

    print '[Parsing', url, '...]'
    xmldoc = ''.join (urllib2.urlopen (url))
    root = ET.fromstring (xmldoc)

    jelem = root.find ('doi_record/crossref/journal')
    if jelem is None:
        die ('no <journal> element as expected in UnixRef XML for %s', doi)

    try:
        info['authors'] = [_translate_unixref_name (p) for p in
                           jelem.findall ('journal_article/contributors/person_name')]
    except:
        pass

    try:
        info['title'] = ' '.join (t.strip () for t in
                                  jelem.find ('journal_article/titles/title').itertext ())
    except:
        pass

    try:
        info['year'] = int (jelem.find ('journal_issue/publication_date/year').text)
    except:
        pass

    return info


_atom_ns = '{http://www.w3.org/2005/Atom}'
_arxiv_ns = '{http://arxiv.org/schemas/atom}'


def autolearn_arxiv (arxiv):
    import xml.etree.ElementTree as ET

    url = 'http://export.arxiv.org/api/query?id_list=' + urlquote (arxiv)
    info = {'arxiv': arxiv, 'keep': 0} # because we're autolearning

    # XXX sad to be not doing this incrementally, but Py 2.x doesn't
    # seem to have an incremental parser built in.

    print '[Parsing', url, '...]'
    xmldoc = ''.join (urllib2.urlopen (url))
    root = ET.fromstring (xmldoc)
    ent = root.find (_atom_ns + 'entry')

    try:
        info['abstract'] = ent.find (_atom_ns + 'summary').text
    except:
        pass

    try:
        info['authors'] = [_translate_arxiv_name (a) for a in
                           ent.findall (_atom_ns + 'author')]
    except:
        pass

    try:
        info['doi'] = ent.find (_arxiv_ns + 'doi').text
    except:
        pass

    try:
        info['title'] = ent.find (_atom_ns + 'title').text
    except:
        pass

    try:
        info['year'] = int (ent.find (_atom_ns + 'published').text[:4])
    except:
        pass

    if 'doi' in info:
        info['bibcode'] = doi_to_maybe_bibcode (info['doi'])

    return info


# Searching ADS

def _run_ads_search (searchterms, filterterms):
    # TODO: access to more API args
    import urllib, json

    apikey = BibConfig ().get_or_die ('api-keys', 'ads')

    q = [('q', ' '.join (searchterms)),
         ('dev_key', apikey)]

    for ft in filterterms:
        q.append (('filter', ft))

    url = 'http://adslabs.org/adsabs/api/search?' + urllib.urlencode (q)
    return json.load (urllib2.urlopen (url))


def parse_search (interms):
    """We go to the trouble of parsing searches ourselves because ADS's syntax
    is quite verbose. Terms we support:

    (integer) -> year specification
       if this year is 2014, 16--99 are treated as 19NN,
       and 00--15 is treated as 20NN (for "2015 in prep" papers)
       Otherwise, treated as a full year.
    """

    outterms = []
    bareword = None

    from time import localtime
    thisyear = localtime ()[0]
    next_twodigit_year = (thisyear + 1) % 100

    for interm in interms:
        try:
            asint = int (interm)
        except ValueError:
            pass
        else:
            if asint < 100:
                if asint > next_twodigit_year:
                    outterms.append (('year', asint + (thisyear // 100 - 1) * 100))
                else:
                    outterms.append (('year', asint + (thisyear // 100) * 100))
            else:
                outterms.append (('year', asint))
            continue

        # It must be the bareword
        if bareword is None:
            bareword = interm
            continue

        die ('searches only support a single "bare word" right now')

    if bareword is not None:
        outterms.append (('surname', bareword)) # note the assumption here

    return outterms


def search_ads (terms, raw=False):
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

    r = _run_ads_search (adsterms, ['database:astronomy']) # XXX more hardcoding

    if raw:
        out = codecs.getwriter ('utf-8') (sys.stdout)
        json.dump (r, out, ensure_ascii=False, indent=2, separators=(',', ': '))
        return

    maxnfaslen = 0
    maxbclen = 0
    info = []

    for item in r['results']['docs'][:20]:
        # year isn't important since it's embedded in bibcode.
        title = item['title'][0] # not sure why this is a list?
        bibcode = item['bibcode']
        nfas = normalize_surname (parse_name (_translate_ads_name (item['author'][0]))[1])

        maxnfaslen = max (maxnfaslen, len (nfas))
        maxbclen = max (maxbclen, len (bibcode))
        info.append ((bibcode, nfas, title))

    ofs = maxnfaslen + maxbclen + 4

    for bc, nfas, title in info:
        print '%*s  %*s  ' % (maxbclen, bc, maxnfaslen, nfas),
        print_truncated (title, ofs)


# Text export/import

def text_export_one (db, pub, write, width):
    # Title and year
    if pub.title is None:
        write ('--no title--\n')
    else:
        print_linewrapped (pub.title, width=width, write=write)
    if pub.year is None:
        write ('--no year--\n')
    else:
        write (str (pub.year))
        write ('\n')
    write ('\n')

    # Unique identifiers
    write ('arxiv = ')
    write (pub.arxiv or '')
    write ('\n')
    write ('bibcode = ')
    write (pub.bibcode or '')
    write ('\n')
    write ('doi = ')
    write (pub.doi or '')
    write ('\n')
    for (nick, ) in db.execute ('SELECT nickname FROM nicknames WHERE pubid == ? '
                                'ORDER BY nickname asc', (pub.id, )):
        write ('nick = ')
        write (nick)
        write ('\n')
    write ('\n')

    # Authors
    anyauth = False
    for given, family in db.get_pub_authors (pub.id):
        write (encode_name (given, family))
        write ('\n')
        anyauth = True
    if not anyauth:
        write ('--no authors--\n')
    firsteditor = True
    for given, family in db.get_pub_authors (pub.id, AUTHTYPE_EDITOR):
        if firsteditor:
            write ('--editors--\n')
            firsteditor = False
        write (encode_name (given, family))
        write ('\n')
    write ('\n')

    # Reference info
    if pub.refdata is None:
        write ('--no reference data--\n')
    else:
        rd = json.loads (pub.refdata)

        btype = rd.pop ('_type')
        write ('@')
        write (btype)
        write ('\n')

        for k in sorted (rd.iterkeys ()):
            write (k)
            write (' = ')
            write (rd[k])
            write ('\n')
    write ('\n')

    # Abstract
    if pub.abstract is None:
        write ('--no abstract--\n')
    else:
        print_linewrapped (pub.abstract, width=width, write=write, maxwidth=72)
    write ('\n')

    # TODO: notes, lists


def _import_get_chunk (stream, gotoend=False):
    lines = []

    for line in stream:
        line = line.strip ()

        if not len (line) and not gotoend:
            return lines

        lines.append (line)

    while len (lines) and not len (lines[-1]):
        lines = lines[:-1]

    return lines


def text_import_one (stream):
    info = {}

    # title / year
    c = _import_get_chunk (stream)

    if len (c) < 2:
        die ('title/year chunk must contain at least two lines')

    info['title'] = squish_spaces (' '.join (c[:-1]))
    if info['title'].startswith ('--'):
        del info['title']

    if not c[-1].startswith ('--'):
        try:
            info['year'] = int (c[-1])
        except Exception as e:
            die ('publication year must be an integer or "--no year--"; '
                 'got "%s"', c[-1])

    # identifiers
    c = _import_get_chunk (stream)
    info['nicknames'] = []

    for line in c:
        if '=' not in line:
            die ('identifier lines must contain "=" signs; got "%s"', line)
        k, v = line.split ('=', 1)
        k = k.strip ()
        v = v.strip ()

        if not v:
            continue

        if k == 'arxiv':
            info['arxiv'] = v
        elif k == 'bibcode':
            info['bibcode'] = v
        elif k == 'doi':
            info['doi'] = v
        elif k == 'nick':
            info['nicknames'].append (v)
        else:
            die ('unexpected identifier kind "%s"', k)

    # authors
    c = _import_get_chunk (stream)
    namelist = info['authors'] = []

    for line in c:
        # This "--" flag must be exact for --editors-- to work
        if line == '--no authors--':
            pass
        elif line == '--editors--':
            namelist = info['editors'] = []
        else:
            namelist.append (line)

    # reference data
    c = _import_get_chunk (stream)

    if not c[0].startswith ('--'):
        rd = info['refdata'] = {}

        if c[0][0] != '@':
            die ('reference data chunk must begin with an "@"; got "%s"', c[0])
        rd['_type'] = c[0][1:]

        for line in c[1:]:
            if '=' not in line:
                die ('ref data lines must contain "=" signs; got "%s"', line)
            k, v = line.split ('=', 1)
            k = k.strip ()
            v = v.strip ()
            rd[k] = v

    # abstract
    c = _import_get_chunk (stream, gotoend=True)
    abs = ''
    spacer = ''

    for line in c:
        if not len (line):
            spacer = '\n'
        elif line == '--no abstract--':
            pass # exact match here too; legitimate lines could start with '--'
        else:
            abs += spacer + line
            spacer = ' '

    if len (abs):
        info['abstract'] = abs

    return info


# Bibtex export
# TODO: styles defined in a support file or something.

class BibtexStyleBase (object):
    include_doi = True
    include_title = False
    issn_name_map = None
    normalize_pages = False


class ApjBibtexStyle (BibtexStyleBase):
    normalize_pages = True

    def __init__ (self):
        inm = {}

        for line in datastream ('apj-issnmap.txt'):
            line = line.split ('#')[0].strip ().decode ('utf-8')
            if not len (line):
                continue

            issn, jname = line.split (None, 1)
            inm[issn] = unicode_to_latex (jname)

        self.issn_name_map = inm


bibtex_styles = {'apj': ApjBibtexStyle}


def _setup_unicode_to_latex ():
    # XXX XXX even worse, don't want to modularize just yet
    import unicode_to_latex
    return unicode_to_latex.unicode_to_latex

    # XXX XXX not reached!
    # XXX fixme annoying to be duplicating my unicode_to_latex.py.
    from .hacked_bibtexparser.latexenc import unicode_to_latex as u2l

    table = dict ((ord (k), unicode (v))
                  for k, v in u2l
                  if len (k) == 1)

    del table[ord (u' ')]

    return lambda u: u.translate (table).encode ('ascii')


unicode_to_latex = _setup_unicode_to_latex ()


def bibtexify_name (style, name):
    given, family = name

    fbits = family.rsplit (',', 1)

    if len (fbits) > 1:
        return '{%s}, %s, %s' % (unicode_to_latex (fbits[0]),
                                 unicode_to_latex (fbits[1]),
                                 unicode_to_latex (given))

    return '{%s}, %s' % (unicode_to_latex (fbits[0]),
                         unicode_to_latex (given))


def bibtexify_names (style, names):
    return ' and '.join (bibtexify_name (style, n) for n in names)


def bibtexify_one (db, style, pub):
    """Returns a dict in which the values are already latex-encoded.
    '_type' is the bibtex type, '_ident' is the bibtex identifier."""

    rd = json.loads (pub.refdata)

    for k in rd.keys ():
        rd[k] = unicode_to_latex (rd[k])

    names = list (db.get_pub_authors (pub.id, AUTHTYPE_AUTHOR))
    if len (names):
        rd['author'] = bibtexify_names (style, names)

    names = list (db.get_pub_authors (pub.id, AUTHTYPE_EDITOR))
    if len (names):
        rd['editor'] = bibtexify_names (style, names)

    if style.include_doi and pub.doi is not None:
        rd['doi'] = unicode_to_latex (pub.doi)

    if style.issn_name_map is not None and 'issn' in rd:
        ltxjname = style.issn_name_map.get (rd['issn'])
        if ltxjname is not None:
            rd['journal'] = ltxjname

    if style.normalize_pages and 'pages' in rd:
        p = rd['pages'].split ('--')[0]
        if p[-1] == '+':
            p = p[:-1]
        rd['pages'] = p

    if style.include_title and pub.title is not None:
        rd['title'] = unicode_to_latex (pub.title)

    if pub.year is not None:
        rd['year'] = str (pub.year)


    return rd


def write_bibtexified (write, btdata):
    """This will mutate `btdata`."""

    bttype = btdata.pop ('_type')
    btid = btdata.pop ('_ident')

    write ('@')
    write (bttype)
    write ('{')
    write (btid)

    for k in sorted (btdata.iterkeys ()):
        write (',\n  ')
        write (k)
        write (' = {')
        write (btdata[k])
        write ('}')

    write ('\n}\n')


# UI subroutines

def print_generic_listing (db, pub_seq):
    info = []
    maxnfaslen = 0
    maxnicklen = 0

    # TODO: number these, and save the results in a table so one can write
    # "bib read %1" to read the top item of the most recent listing.

    for pub in pub_seq:
        nfas = pub.nfas or '(no author)'
        year = pub.year or '????'
        title = pub.title or '(no title)'
        nick = db.choose_pub_nickname (pub.id) or ''

        if isinstance (year, int):
            year = '%04d' % year

        info.append ((nfas, year, title, nick))
        maxnfaslen = max (maxnfaslen, len (nfas))
        maxnicklen = max (maxnicklen, len (nick))

    ofs = maxnfaslen + maxnicklen + 9

    for nfas, year, title, nick in info:
        print '%*s.%s  %*s  ' % (maxnfaslen, nfas, year, maxnicklen, nick),
        print_truncated (title, ofs)
