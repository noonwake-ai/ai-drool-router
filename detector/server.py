# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""INPUT: read-only public projection. OUTPUT: static dashboard and sandboxed HTML. POS: credential-free HTTP service."""
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .controls import ControlConflict, Controls
from .supplier_prices import Prices, project_price
APP_CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
ARTIFACT_CSP = "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,request,client_address,server):
        super().__init__(request,client_address,server,directory=str(server.dist))

    def log_message(self,*args):
        pass

    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Robots-Tag','noindex, nofollow, noarchive')
        self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=(), payment=()')
        self.send_header('Content-Security-Policy', ARTIFACT_CSP if getattr(self,'is_artifact',False) else APP_CSP)
        if not getattr(self,'is_artifact',False):
            self.send_header('X-Frame-Options','DENY')
        super().end_headers()

    def send_file(self,path,content_type,cache='no-store'):
        try:
            data=path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control',cache)
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        if self.command!='HEAD':
            self.wfile.write(data)

    def send_json(self, value, status=200):
        data=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        if self.command!='HEAD':
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.is_artifact=False
        path=urlsplit(self.path).path
        if path=='/healthz':
            data=json.dumps({'status':'ok','state_ready':(self.server.public/'state.json').exists()}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            if self.command!='HEAD':
                self.wfile.write(data)
            return
        if path=='/api/state':
            try:
                state=json.loads((self.server.public/'state.json').read_text())
                controls=self.server.controls.read()
                prices=self.server.prices.read()
                for account in state['accounts']:
                    account.update(controls.get(account['id'], {'paused':False,'resume_at':0}))
                    account['price']=prices.get(account['id'],project_price())
                state['controls_enabled']=self.server.controls_enabled
            except (ValueError,OSError,KeyError):
                return self.send_json({'error':'STATE_UNAVAILABLE'},503)
            return self.send_json(state)
        match=re.fullmatch(r'/(api/runs|artifacts)/([a-f0-9]{32})(\.html)?',path)
        if match:
            kind,ident,ext=match.groups()
            if (kind=='artifacts') != bool(ext):
                return self.send_error(404)
            detail_path=self.server.public/(ident+'.json')
            try:
                detail=json.loads(detail_path.read_text())
            except (ValueError,OSError):
                return self.send_error(404)
            if detail.get('slot',0)<time.time()-86400:
                return self.send_error(410,'History expired')
            if kind=='artifacts':
                if detail.get('status')!='pass' or detail.get('kind')!='drawing':
                    return self.send_error(404)
                self.is_artifact=True
                return self.send_file(self.server.public/(ident+'.html'),'text/html; charset=utf-8')
            return self.send_file(detail_path,'application/json; charset=utf-8')
        if path in ('/','/index.html'):
            return self.send_file(self.server.dist/'index.html','text/html; charset=utf-8')
        if re.fullmatch(r'/assets/[a-zA-Z0-9._-]+\.(js|css|woff2|png|svg|jpg|jpeg)',path):
            target=self.server.dist/path.lstrip('/')
            return self.send_file(target,self.guess_type(str(target)),'public, max-age=31536000, immutable')
        return self.send_error(404)

    def do_POST(self):
        self.is_artifact=False
        match=re.fullmatch(r'/api/accounts/([a-f0-9]{12})/(pause|price)',urlsplit(self.path).path)
        if self.command!='POST' or not match or not self.server.controls_enabled:
            return self.send_error(405,'Read-only service')
        origin=self.headers.get('Origin','')
        try:
            parsed_origin=urlsplit(origin)
        except ValueError:
            return self.send_json({'error':'ORIGIN_REJECTED'},403)
        if not origin or parsed_origin.netloc!=self.headers.get('Host') or parsed_origin.scheme not in ('http','https'):
            return self.send_json({'error':'ORIGIN_REJECTED'},403)
        if self.headers.get('Content-Type')!='application/json' or self.headers.get('Transfer-Encoding'):
            return self.send_json({'error':'JSON_REQUIRED'},415)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=(4096 if match[2]=='price' else 512):
                return self.send_json({'error':'INVALID_BODY_SIZE'},413)
            self.connection.settimeout(5)
            body=json.loads(self.rfile.read(length))
            state=json.loads((self.server.public/'state.json').read_text())
            if match[1] not in {a['id'] for a in state['accounts']}:
                return self.send_json({'error':'UNKNOWN_ACCOUNT'},404)
            if match[2]=='pause':
                if not isinstance(body,dict) or set(body)!={'paused','expected_paused'} or any(type(v) is not bool for v in body.values()):
                    raise ValueError('INVALID_CONTROL')
                result=self.server.controls.set_paused(match[1],body['paused'],body['expected_paused'])
            else:
                if isinstance(body,dict) and set(body)=={'config','expected_revision'}:
                    result=self.server.prices.set_config(match[1],body['config'],body['expected_revision'])
                elif isinstance(body,dict) and set(body)=={'multiplier','expected_multiplier'}:
                    result=self.server.prices.set(match[1],body['multiplier'],body['expected_multiplier'])
                else:
                    raise ValueError('INVALID_PRICE')
            return self.send_json(result)
        except ControlConflict:
            return self.send_json({'error':'CONTROL_CHANGED'},409)
        except (ValueError,TypeError):
            return self.send_json({'error':'INVALID_CONTROL'},400)
        except OSError:
            return self.send_json({'error':'CONTROL_UNAVAILABLE'},503)

    do_PUT=do_POST
    do_DELETE=do_POST
    do_PATCH=do_POST


def make_server(host,port,dist,public,controls_enabled=False):
    server=ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads=True
    server.dist=Path(dist)
    server.public=Path(public)
    server.controls=Controls(server.public.parent/'controls')
    server.prices=Prices(server.public.parent/'controls')
    server.controls_enabled=controls_enabled
    return server


if __name__=='__main__':
    from . import config
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default=config.get('web.host') or '127.0.0.1')
    parser.add_argument('--port',type=int,default=int(config.get('web.port') or 4191))
    # The Vite build lands in web/dist; deployments may override this with --dist.
    parser.add_argument('--dist',default=str(Path(__file__).resolve().parent.parent/'web'/'dist'))
    parser.add_argument('--public',default=str(config.data_dir()/'public'))
    parser.add_argument('--enable-public-controls',action='store_true',help='Only enable after explicitly authorizing anonymous pause/resume')
    args=parser.parse_args()
    if not args.enable_public_controls and config.get('web.public_controls'):
        args.enable_public_controls = True
    make_server(args.host,args.port,args.dist,args.public,args.enable_public_controls).serve_forever()
