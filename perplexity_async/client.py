import re
import ssl
import json
import socket
import random
import asyncio
import mimetypes
from uuid import uuid4
from threading import Thread
from curl_cffi import requests, CurlMime
from websocket import WebSocketApp

from .emailnator import Emailnator


# https://dev.to/akarshan/asynchronous-python-magic-how-to-create-awaitable-constructors-with-asyncmixin-18j5
# https://web.archive.org/web/20230915163459/https://dev.to/akarshan/asynchronous-python-magic-how-to-create-awaitable-constructors-with-asyncmixin-18j5
class AsyncMixin:
    def __init__(self, *args, **kwargs):
        self.__storedargs = args, kwargs
        self.async_initialized = False
        
    async def __ainit__(self, *args, **kwargs):
        pass
    
    async def __initobj(self):
        assert not self.async_initialized
        self.async_initialized = True
        # pass the parameters to __ainit__ that passed to __init__
        await self.__ainit__(*self.__storedargs[0], **self.__storedargs[1])
        return self
    
    def __await__(self):
        return self.__initobj().__await__()

class Client(AsyncMixin):
    '''
    A client for interacting with the Perplexity AI API.
    '''
    async def __ainit__(self, headers, cookies, own=False):
        self.session = requests.AsyncSession(headers=headers, cookies=cookies)
        self.own = own
        self.copilot = 0 if not own else float('inf')
        self.file_upload = 0 if not own else float('inf')
        self.message_counter = 1
        self.signin_regex = re.compile(r'"(https://www\.perplexity\.ai/api/auth/callback/email\?callbackUrl=.*?)"')
        self.last_answer = None
        self.last_file_upload_info = None
        self.timestamp = format(random.getrandbits(32), '08x')
        self.sid = json.loads((await self.session.get(f'https://www.perplexity.ai/socket.io/?EIO=4&transport=polling&t={self.timestamp}')).text[1:])['sid']
        
        assert (await self.session.post(f'https://www.perplexity.ai/socket.io/?EIO=4&transport=polling&t={self.timestamp}&sid={self.sid}', data='40{"jwt":"anonymous-ask-user"}')).text == 'OK'
        
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.sock = context.wrap_socket(socket.create_connection(('www.perplexity.ai', 443)), server_hostname='www.perplexity.ai')
        
        self.ws = WebSocketApp(
            url=f'wss://www.perplexity.ai/socket.io/?EIO=4&transport=websocket&sid={self.sid}',
            header={'User-Agent': self.session.headers['User-Agent']},
            cookie='; '.join([f'{key}={value}' for key, value in self.session.cookies.get_dict().items()]),
            on_open=lambda ws: (ws.send('2probe'), ws.send('5')),
            on_message=self._on_message,
            on_error=lambda ws, error: print(f'Websocket Error: {error}'),
            socket=self.sock
        )
        
        Thread(target=self.ws.run_forever, daemon=True).start()
        
        while not (self.ws.sock and self.ws.sock.connected):
            await asyncio.sleep(0.01)
    
    async def create_account(self, headers, cookies):
        '''
        Function to create a new account
        '''
        while True:
            try:
                emailnator_cli = await Emailnator(headers, cookies)
                
                resp = await self.session.post('https://www.perplexity.ai/api/auth/signin/email', data={
                    'email': emailnator_cli.email,
                    'csrfToken': self.session.cookies.get_dict()['next-auth.csrf-token'].split('%')[0],
                    'callbackUrl': 'https://www.perplexity.ai/',
                    'json': 'true'
                })
                
                if resp.ok:
                    new_msgs = await emailnator_cli.reload(wait_for=lambda x: x['subject'] == 'Sign in to Perplexity', timeout=20)
                    
                    if new_msgs:
                        break
                else:
                    print('Perplexity account creating error:', resp)
            
            except:
                pass
        
        msg = emailnator_cli.get(func=lambda x: x['subject'] == 'Sign in to Perplexity')
        new_account_link = self.signin_regex.search(await emailnator_cli.open(msg['messageID'])).group(1)
        
        await self.session.get(new_account_link)
        
        self.copilot = 5
        self.file_upload = 10
        
        self.ws.close()
        del self.sock
        
        self.sid = json.loads((await self.session.get(f'https://www.perplexity.ai/socket.io/?EIO=4&transport=polling&t={self.timestamp}')).text[1:])['sid']
        
        assert (await self.session.post(f'https://www.perplexity.ai/socket.io/?EIO=4&transport=polling&t={self.timestamp}&sid={self.sid}', data='40{"jwt":"anonymous-ask-user"}')).text == 'OK'
        
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.sock = context.wrap_socket(socket.create_connection(('www.perplexity.ai', 443)), server_hostname='www.perplexity.ai')
        
        self.ws = WebSocketApp(
            url=f'wss://www.perplexity.ai/socket.io/?EIO=4&transport=websocket&sid={self.sid}',
            header={'User-Agent': self.session.headers['User-Agent']},
            cookie='; '.join([f'{key}={value}' for key, value in self.session.cookies.get_dict().items()]),
            on_open=lambda ws: (ws.send('2probe'), ws.send('5')),
            on_message=self._on_message,
            on_error=lambda ws, error: print(f'Websocket Error: {error}'),
            socket=self.sock
        )
        
        Thread(target=self.ws.run_forever).start()
        
        while not (self.ws.sock and self.ws.sock.connected):
            await asyncio.sleep(0.01)
        
        return True
    
    def _on_message(self, ws, message):
        '''
        Websocket message handler
        '''
        if message == '2':
            ws.send('3')
        
        elif message.startswith(str(self.message_counter + 430)):
            response = json.loads(message[len(str(self.message_counter + 430)):])[0]
            
            if 'text' in response:
                response['text'] = json.loads(response['text'])
                self.last_answer = response
            elif 'fields' in response:
                self.last_file_upload_info = response
        
        elif message.startswith('42'):
            self.last_answer = json.loads(message[2:])[1]
    
    async def search(self, query, mode='auto', sources=['web'], files={}, stream=False, language='en-US', follow_up=None, incognito=False):
        '''
        Query function
        '''
        assert mode in ['auto', 'pro', 'deep research', 'r1', 'o3-mini'], 'Search modes -> ["auto", "pro", "deep research", "r1", "o3-mini"]'
        assert all([source in ('web', 'scholar', 'social') for source in sources]), 'Sources -> ["web", "scholar", "social"]'
        assert self.copilot > 0 if mode in ['pro', 'deep research', 'r1', 'o3-mini'] else True, 'You have used all of your enhanced (pro) queries'
        assert self.file_upload - len(files) >= 0 if files else True, f'You have tried to upload {len(files)} files but you have {self.file_upload} file upload(s) remaining.'
        
        self.copilot = self.copilot - 1 if mode in ['pro', 'r1', 'o3-mini'] else self.copilot
        self.file_upload = self.file_upload - len(files) if files else self.file_upload
        self.message_counter += 1
        self.last_answer = None
        self.last_file_upload_info = None
        
        uploaded_files = []
        
        for filename, file in files.items():
            self.ws.send(f'{self.message_counter + 420}' + json.dumps([
                'get_upload_url',
                {
                    'content_type': mimetypes.guess_type(filename)[0],
                    'filename': filename,
                    'source': 'default',
                    'version': '2.18'
                }
            ]))
            
            while not self.last_file_upload_info:
                await asyncio.sleep(0.01)
            self.message_counter += 1
            
            if not self.last_file_upload_info['success']:
                raise Exception('File upload error', self.last_file_upload_info)
            
            mp = CurlMime()
            
            for key, value in self.last_file_upload_info['fields'].items():
                mp.addpart(name=key, data=value)
            
            mp.addpart(name='file', content_type=mimetypes.guess_type(filename)[0], filename=filename, data=file)
            
            upload_resp = await self.session.post(self.last_file_upload_info['url'], multipart=mp)
            
            if not upload_resp.ok:
                raise Exception('File upload error', upload_resp)
            
            uploaded_files.append(self.last_file_upload_info['url'] + self.last_file_upload_info['fields']['key'].replace('${filename}', filename))
        
        self.ws.send(f'{self.message_counter + 420}' + json.dumps([
            'perplexity_ask',
            query,
            {
                'attachments': uploaded_files + follow_up['attachments'] if follow_up else uploaded_files,
                'frontend_context_uuid': str(uuid4()),
                'frontend_uuid': str(uuid4()),
                'is_incognito': incognito,
                'language': language,
                'last_backend_uuid': follow_up['backend_uuid'] if follow_up else None,
                'mode': 'concise' if mode == 'auto' else 'copilot',
                'model_preference': {'auto': None, 'pro': None, 'deep research': 'pplx_alpha', 'r1': 'r1', 'o3-mini': 'o3mini'}[mode],
                'source': 'default',
                'sources': sources,
                'version': '2.18'
            }
        ]))
        
        async def stream_response(self):
            answer = None
            
            while True:
                if self.last_answer != answer:
                    answer = self.last_answer
                    yield answer
                
                if self.last_answer['status'] == 'completed':
                    answer = self.last_answer
                    self.last_answer = None
                    
                    yield answer
                    return
                
                await asyncio.sleep(0.01)
        
        while True:
            if self.last_answer and stream:
                return stream_response(self)
            
            elif self.last_answer and self.last_answer['status'] == 'completed':
                answer = self.last_answer
                self.last_answer = None
                
                return answer
            
            await asyncio.sleep(0.01)