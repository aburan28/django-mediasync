from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.dispatch import receiver
from django.template import Context, Template
from hashlib import md5
import glob
import httplib
import itertools
import os
import re
import time
import unittest

from mediasync import backends, JS_MIMETYPES, listdir_recursive
from mediasync.backends import BaseClient
from mediasync.conf import msettings
from mediasync.signals import pre_sync, post_sync, sass_receiver
import mediasync
import mimetypes

PWD = os.path.abspath(os.path.dirname(__file__))

EXPIRES_RE = re.compile(r'^\w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2} GMT$')

def readfile(path):
    f = open(path, 'r')
    content = f.read()
    f.close()
    return content

class Client(BaseClient):
    
    def __init__(self, *args, **kwargs):
        super(Client, self).__init__(*args, **kwargs)
    
    def put(self, filedata, content_type, remote_path, force=False):
        if hasattr(self, 'put_callback'):
            return self.put_callback(filedata, content_type, remote_path, force)
        else:
            return True
        
    def remote_media_url(self, with_ssl=False):
        return ('https' if with_ssl else 'http') + "://localhost"

#
# tests
#

class BackendTestCase(unittest.TestCase):
    
    def setUp(self):
        msettings['BACKEND'] = 'not.a.backend'
        
    def tearDown(self):
        msettings['BACKEND'] = 'mediasync.backends.dummy'

    def testInvalidBackend(self):
        self.assertRaises(ImproperlyConfigured, backends.client)

class MockClientTestCase(unittest.TestCase):
    
    def setUp(self):
        msettings['BACKEND'] = 'mediasync.tests.tests'
        msettings['PROCESSORS'] = []
        msettings['SERVE_REMOTE'] = True
        msettings['JOINED'] = {
            'css/joined.css': ('css/1.css', 'css/2.css'),
            'js/joined.js': ('js/1.js', 'js/2.js'),
        }
        self.client = backends.client()
    
    def tearDown(self):
        msettings['JOINED'] = {}
    
    def testLocalMediaURL(self):
        self.assertEqual(self.client.get_local_media_url(), "/media/")
    
    def testMediaRoot(self):
        root = getattr(settings, 'STATIC_ROOT', None)
        if root is None:
            root = getattr(settings, 'MEDIA_ROOT', None)
        self.assertEqual(self.client.get_media_root(), root)
    
    def testMediaURL(self):
        self.assertEqual(self.client.media_url(with_ssl=False), "http://localhost")
        self.assertEqual(self.client.media_url(with_ssl=True), "https://localhost")
    
    def testSyncableDir(self):
        # not syncable
        self.assertFalse(mediasync.is_syncable_dir(".test"))
        self.assertFalse(mediasync.is_syncable_dir("_test"))
        # syncable
        self.assertTrue(mediasync.is_syncable_dir("test"))
        self.assertTrue(mediasync.is_syncable_dir("1234"))
    
    def testSyncableFile(self):
        # not syncable
        self.assertFalse(mediasync.is_syncable_file(".test"))
        self.assertFalse(mediasync.is_syncable_file("_test"))
        # syncable
        self.assertTrue(mediasync.is_syncable_file("test"))
        self.assertTrue(mediasync.is_syncable_file("1234"))
    
    def testDirectoryListing(self):
        allowed_files = [
            'css/1.css',
            'css/2.css',
            'css/3.scss',
            'img/black.png',
            'js/1.js',
            'js/2.js',
        ]
        media_dir = os.path.join(PWD, 'media')
        listed_files = list(mediasync.listdir_recursive(media_dir))
        self.assertListEqual(allowed_files, listed_files)
    
    def testSync(self):
        
        to_sync = {
            'css/1.css': 'text/css',
            'css/2.css': 'text/css',
            'css/3.scss': msettings['DEFAULT_MIMETYPE'],
            'css/joined.css': 'text/css',
            'img/black.png': 'image/png',
            'js/1.js': 'application/javascript',
            'js/2.js': 'application/javascript',
            'js/joined.js': 'application/javascript',
        }
        
        def generate_callback(is_forced):
            def myput(filedata, content_type, remote_path, force=is_forced):
                
                self.assertEqual(content_type, to_sync[remote_path])
                self.assertEqual(force, is_forced)
                
                if remote_path in msettings['JOINED']:
                    original = readfile(os.path.join(PWD, 'media', '_test', remote_path.split('/')[1]))
                else:
                    args = [PWD, 'media'] + remote_path.split('/')
                    original = readfile(os.path.join(*args))
                
                self.assertEqual(filedata, original)
                    
            return myput
        
        # normal sync
        self.client.put_callback = generate_callback(is_forced=False)
        mediasync.sync(self.client, force=False, verbose=False)
        
        # forced sync
        self.client.put_callback = generate_callback(is_forced=True)
        mediasync.sync(self.client, force=True, verbose=False)
        
class S3ClientTestCase(unittest.TestCase):

    def setUp(self):
        
        bucket_hash = md5("%i-%s" % (int(time.time()), os.environ['USER'])).hexdigest()
        self.bucket_name = 'mediasync_test_' + bucket_hash
        
        msettings['BACKEND'] = 'mediasync.backends.s3'
        msettings['AWS_BUCKET'] = self.bucket_name
        msettings['AWS_KEY'] = os.environ['AWS_KEY'] or None
        msettings['AWS_SECRET'] = os.environ['AWS_SECRET'] or None
        msettings['PROCESSORS'] = []
        msettings['SERVE_REMOTE'] = True
        msettings['JOINED'] = {
            'css/joined.css': ('css/1.css', 'css/2.css'),
            'js/joined.js': ('js/1.js', 'js/2.js'),
        }
        
        self.client = backends.client()
    
    def testServeRemote(self):
        
        msettings['SERVE_REMOTE'] = False
        self.assertEqual(backends.client().media_url(), '/media')

        msettings['SERVE_REMOTE'] = True
        self.assertEqual(backends.client().media_url(), 'http://s3.amazonaws.com/%s' % self.bucket_name)
    
    def testSync(self):
        
        # calculate cache control
        cc = "max-age=%i, public" % (self.client.expiration_days * 24 * 3600)
        
        # do a sync then reopen client
        mediasync.sync(self.client, force=True, verbose=False)
        self.client.open()
        conn = self.client.get_connection()
        
        # setup http connection
        http_conn = httplib.HTTPSConnection('s3.amazonaws.com')
        
        # test synced files then delete them
        bucket = conn.get_bucket(self.bucket_name)
        
        static_paths = mediasync.listdir_recursive(os.path.join(PWD, 'media'))
        joined_paths = msettings['JOINED'].iterkeys()
        
        for path in itertools.chain(static_paths, joined_paths):
            
            key = bucket.get_key(path)
            
            if path in msettings['JOINED']:
                args = [PWD, 'media', '_test', path.split('/')[1]]
            else:
                args = [PWD, 'media'] + path.split('/')
            local_content = readfile(os.path.join(*args))

            # compare file content
            self.assertEqual(key.read(), local_content)
            
            # verify checksum
            key_meta = key.get_metadata('mediasync-checksum') or ''
            s3_checksum = key_meta.replace(' ', '+')
            (hexdigest, b64digest) = mediasync.checksum(local_content)
            self.assertEqual(s3_checksum, b64digest)
            
            # do a HEAD request on the file
            http_conn.request('HEAD', "/%s/%s" % (self.bucket_name, path))
            response = http_conn.getresponse()
            response.read()
            
            # verify valid content type
            content_type = mimetypes.guess_type(path)[0] or msettings['DEFAULT_MIMETYPE']
            self.assertEqual(response.getheader("Content-Type", None), content_type)
            
            # check for valid expires headers
            expires = response.getheader("Expires", None)
            self.assertRegexpMatches(expires, EXPIRES_RE)
            
            # check for valid cache control header
            cc_header = response.getheader("Cache-Control", None)
            self.assertEqual(cc_header, cc)
            
            # done with the file, delete it from S3
            key.delete()
            
            if content_type in mediasync.TYPES_TO_COMPRESS:
                
                key = bucket.get_key("%s.gzt" % path)
                
                # do a HEAD request on the file
                http_conn.request('HEAD', "/%s/%s.gzt" % (self.bucket_name, path))
                response = http_conn.getresponse()
                response.read()
                
                key_meta = key.get_metadata('mediasync-checksum') or ''
                s3_checksum = key_meta.replace(' ', '+')
                self.assertEqual(s3_checksum, b64digest)
                
                key.delete()
        
        http_conn.close()
        
        # wait a moment then delete temporary bucket
        time.sleep(2)
        conn.delete_bucket(self.bucket_name)
        
        # close client
        self.client.close()
    
    def testMissingBucket(self):
        del msettings['AWS_BUCKET']
        self.assertRaises(AssertionError, backends.client)

class ProcessorTestCase(unittest.TestCase):

    def setUp(self):
        msettings['SERVE_REMOTE'] = True
        msettings['BACKEND'] = 'mediasync.tests.tests'
        msettings['PROCESSORS'] = (
            'mediasync.processors.slim.css_minifier',
            'mediasync.processors.slim.js_minifier',
            lambda fd, ct, rp, r: fd.upper(),
        )
        self.client = backends.client()
    
    def testJSProcessor(self):
        
        try:
            import slimmer
        except ImportError:
            self.skipTest("slimmer not installed, skipping test")
        
        content = """var foo = function() {
            alert(1);
        };"""
        
        ct = 'text/javascript'
        procd = self.client.process(content, ct, 'test.js')
        self.assertEqual(procd, 'VAR FOO = FUNCTION(){ALERT(1)};')
    
    def testCSSProcessor(self):
        
        try:
            import slimmer
        except ImportError:
            self.skipTest("slimmer not installed, skipping test")
        
        content = """html {
            border: 1px solid #000000;
            font-family: "Helvetica", "Arial", sans-serif;
        }"""

        ct = 'text/css'
        procd = self.client.process(content, ct, 'test.css')
        self.assertEqual(procd, 'HTML{BORDER:1PX SOLID #000;FONT-FAMILY:"HELVETICA","ARIAL",SANS-SERIF}')
    
    def testCustomProcessor(self):
        procd = self.client.process('asdf', 'text/plain', 'asdf.txt')
        self.assertEqual(procd, "ASDF")

class ClosureCompilerTestCase(unittest.TestCase):
    
    def setUp(self):
        msettings['SERVE_REMOTE'] = True
        msettings['BACKEND'] = 'mediasync.tests.tests'
        msettings['PROCESSORS'] = (
            'mediasync.processors.closurecompiler.compile',
        )
        self.client = backends.client()
    
    def testCompiler(self):
        
        content = """var foo = function() {
            alert(1);
        };"""
        
        for ct in JS_MIMETYPES:
            procd = self.client.process(content, ct, 'test.js')
            self.assertEqual(procd, 'var foo=function(){alert(1)};\n')
    
    def testNotJavascript(self):
        
        content = """html {
            border: 1px solid #000000;
            font-family: "Helvetica", "Arial", sans-serif;
        }"""
        
        procd = self.client.process(content, 'text/css', 'test.css')
        self.assertEqual(procd, content)

class SignalTestCase(unittest.TestCase):
    
    def setUp(self):
        msettings['BACKEND'] = 'mediasync.tests.tests'
        self.client = backends.client()
    
    def tearDown(self):
        root = msettings['STATIC_ROOT']
        for filename in glob.glob(os.path.join(root, "*/*.s[ac]ss")):
            path = filename[:-4] + "css"
            if os.path.exists(path):
                os.unlink(path)
    
    def testSyncSignals(self):
        
        self.client.called_presync = False
        self.client.called_postsync = False
        
        @receiver(pre_sync, weak=False)
        def presync_receiver(sender, **kwargs):
            self.assertEqual(self.client, sender)
            sender.called_presync = True
        
        @receiver(post_sync, weak=False)
        def postsync_receiver(sender, **kwargs):
            self.assertEqual(self.client, sender)
            sender.called_postsync = True
            
        mediasync.sync(self.client, force=True, verbose=False)
        
        self.assertTrue(self.client.called_presync)
        self.assertTrue(self.client.called_postsync)
    
    def testSassReceiver(self):
        
        pre_sync.connect(sass_receiver)
        
        mediasync.sync(self.client, force=True, verbose=False)
        
        root = msettings['STATIC_ROOT']
        
        for sass_path in glob.glob(os.path.join(root, "*/*.s[ac]ss")):
            css_path = sass_path[:-4] + "css"
            self.assertTrue(os.path.exists(css_path))

class TemplateTagTestCase(unittest.TestCase):
    
    def setUp(self):
        msettings['BACKEND'] = 'mediasync.tests.tests'
        msettings['DOCTYPE'] = 'html5'
        self.client = backends.client()
    
    def testMediaURLTag(self):
        
        pathvar = 'images/logo.png'
        c = Context({'pathvar': pathvar})
        
        # base media url
        t = Template('{% load media %}{% media_url %}')
        self.assertEqual(t.render(c), "http://localhost")
        
        # media url with string argument
        t = Template('{%% load media %%}{%% media_url "%s" %%}' % pathvar)
        self.assertEqual(t.render(c), "http://localhost/images/logo.png")
        
        # media url with variable argument
        t = Template('{% load media %}{% media_url pathvar %}')
        self.assertEqual(t.render(c), "http://localhost/images/logo.png")
    
    def testCSSTag(self):
        
        pathvar = 'styles/reset.css'
        c = Context({'pathvar': pathvar})
        
        # css tag with string argument
        t = Template('{%% load media %%}{%% css "%s" %%}' % pathvar)
        self.assertEqual(
            t.render(c),
            '<link rel="stylesheet" href="http://localhost/%s" media="screen, projection">' % pathvar)

        # css tag with string argument and explicit media type
        t = Template('{%% load media %%}{%% css "%s" "tv" %%}' % pathvar)
        self.assertEqual(
            t.render(c),
            '<link rel="stylesheet" href="http://localhost/%s" media="tv">' % pathvar)
        
        # css tag with variable argument
        t = Template('{% load media %}{% css pathvar %}')
        self.assertEqual(
            t.render(c),
            '<link rel="stylesheet" href="http://localhost/%s" media="screen, projection">' % pathvar)

        # css tag with variable argument and explicit media type
        t = Template('{% load media %}{% css pathvar "tv" %}')
        self.assertEqual(
            t.render(c),
            '<link rel="stylesheet" href="http://localhost/%s" media="tv">' % pathvar)
    
    def testJSTag(self):

        pathvar = 'scripts/jquery.js'
        c = Context({'pathvar': pathvar})
        
        # js tag with string argument
        t = Template('{%% load media %%}{%% js "%s" %%}' % pathvar)
        self.assertEqual(
            t.render(c),
            '<script src="http://localhost/%s"></script>' % pathvar)
        
        # js tag with variable argument
        t = Template('{% load media %}{% js pathvar %}')
        self.assertEqual(
            t.render(c),
            '<script src="http://localhost/%s"></script>' % pathvar)
    
    def testMultipleTags(self):
        
        paths = ('scripts/1.js','scripts/2.js')
        c = Context({'paths': paths})
        
        t = Template('{% load media %}{% for path in paths %}{% media_url path %}{% endfor %}')
        self.assertEqual(
            t.render(c),
            'http://localhost/scripts/1.jshttp://localhost/scripts/2.js')
        