# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from decimal import Decimal
import os
import random
import re
import shutil
import threading
import urllib
import urllib.parse

from bitcoinx import Address

from .bitcoin import COIN, is_address_valid
from .i18n import _
from .logs import logs
from .networks import Net
from .util import format_satoshis_plain


logger = logs.get_logger("web")


def BE_from_config(config):
    return config.get('block_explorer', '')

def random_BE():
    possible_keys = [ k for k in Net.BLOCK_EXPLORERS.keys() if k != "system default" ]
    if len(possible_keys):
        return random.choice(possible_keys)

def BE_URL(config, kind, item):
    selected_key = BE_from_config(config)
    if selected_key is None or selected_key not in Net.BLOCK_EXPLORERS:
        selected_key = random_BE()
    be_tuple = Net.BLOCK_EXPLORERS.get(selected_key)
    if not be_tuple:
        return
    url_base, parts = be_tuple
    kind_str = parts.get(kind)
    if kind_str is None:
        return
    if kind == 'addr':
        assert isinstance(item, Address)
        item = item.to_string()
    return "/".join(part for part in (url_base, kind_str, item) if part)

def BE_sorted_list():
    return sorted(Net.BLOCK_EXPLORERS)


def create_URI(addr, amount, message):
    if not isinstance(addr, Address):
        return ""

    query = ['sv']
    if amount:
        query.append('amount=%s'%format_satoshis_plain(amount))
    if message:
        query.append('message=%s'%urllib.parse.quote(message))
    p = urllib.parse.ParseResult(scheme=Net.URI_PREFIX,
                                 netloc='', path=addr.to_string(),
                                 params='', query='&'.join(query), fragment='')
    return urllib.parse.urlunparse(p)


def is_URI(text):
    '''Returns true if the text looks like a URI.  It is not validated, and is not checked to
    be a Bitcoin SV URI.
    '''
    return text.lower().startswith(Net.URI_PREFIX + ':')


class URIError(Exception):
    pass


def parse_URI(uri, on_pr=None):
    if is_address_valid(uri):
        return {'address': uri}

    u = urllib.parse.urlparse(uri)

    # The scheme always comes back in lower case
    pq = urllib.parse.parse_qs(u.query, keep_blank_values=True)
    if u.scheme != Net.URI_PREFIX or 'sv' not in pq:
        raise URIError(_('invalid BitcoinSV URI: {}').format(uri))

    for k, v in pq.items():
        if len(v) != 1:
            raise URIError(_('duplicate query key {0} in BitcoinSV URI {1}').format(k, uri))

    out = {k: v[0] for k, v in pq.items()}

    if is_address_valid(u.path):
        out['address'] = u.path

    if 'amount' in out:
        am = out['amount']
        m = re.match(r'([0-9\.]+)X([0-9])', am)
        if m:
            k = int(m.group(2)) - 8
            amount = Decimal(m.group(1)) * pow(10, k)
        else:
            amount = Decimal(am) * COIN
        out['amount'] = int(amount)
    if 'message' in out:
        out['message'] = out['message']
        out['memo'] = out['message']
    if 'time' in out:
        out['time'] = int(out['time'])
    if 'exp' in out:
        out['exp'] = int(out['exp'])

    payment_url = out.get('r')
    if on_pr and payment_url:
        def get_payment_request_thread():
            from . import paymentrequest
            request = paymentrequest.get_payment_request(payment_url)
            if on_pr:
                on_pr(request)
        t = threading.Thread(target=get_payment_request_thread)
        t.setDaemon(True)
        t.start()

    return out

def check_www_dir(rdir):
    if not os.path.exists(rdir):
        os.mkdir(rdir)
    index = os.path.join(rdir, 'index.html')
    if not os.path.exists(index):
        logger.debug("copying index.html")
        src = os.path.join(os.path.dirname(__file__), 'www', 'index.html')
        shutil.copy(src, index)
    files = [
        "https://code.jquery.com/jquery-1.9.1.min.js",
        "https://raw.githubusercontent.com/davidshimjs/qrcodejs/master/qrcode.js",
        "https://code.jquery.com/ui/1.10.3/jquery-ui.js",
        "https://code.jquery.com/ui/1.10.3/themes/smoothness/jquery-ui.css"
    ]
    for URL in files:
        path = urllib.parse.urlsplit(URL).path
        filename = os.path.basename(path)
        path = os.path.join(rdir, filename)
        if not os.path.exists(path):
            logger.debug("downloading %s", URL)
            urllib.request.urlretrieve(URL, path)
