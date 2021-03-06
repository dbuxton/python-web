import re
import random
import time
import cookielib
import urllib2
import urllib
import mimetypes
import gzip
import StringIO
import urlparse
import collections
import pybloom
import json
import csv
import os.path
import deathbycaptcha

import greenlet
import gevent
from gevent import monkey
from gevent import queue
from gevent import select
import custompool
monkey.patch_all(thread=False)

from lxml import etree
from functools import partial

from urllib import quote_plus

def levenshtein_distance(first, second):
    """Find the Levenshtein distance between two strings."""
    if len(first) > len(second):
        first, second = second, first
    if len(second) == 0:
        return len(first)
    first_length = len(first) + 1
    second_length = len(second) + 1
    distance_matrix = [[0] * second_length for x in range(first_length)]
    for i in range(first_length):
       distance_matrix[i][0] = i
    for j in range(second_length):
       distance_matrix[0][j]=j
    for i in xrange(1, first_length):
        for j in range(1, second_length):
            deletion = distance_matrix[i-1][j] + 1
            insertion = distance_matrix[i][j-1] + 1
            substitution = distance_matrix[i-1][j-1]
            if first[i-1] != second[j-1]:
                substitution += 1
            distance_matrix[i][j] = min(insertion, deletion, substitution)
    return distance_matrix[first_length-1][second_length-1]

class UberIterator(object):
	def __init__(self,objects=None):
		self.objects = []
		self.popped_counter = 0
		if objects is not None:
			self.objects += objects
			
	def __iter__(self):
		return self
		
	def __len__(self):
		return len(self.objects)

	def count(self):
		return len(self.objects) + self.popped_counter
	
	def next(self):
		if len(self.objects):
			self.popped_counter += 1
			return self.objects.pop(0)
		else:
			raise StopIteration
		
	def __add__(self,objects):
		self.objects += list(set(objects))
		return self


class HTTPResponse(object):
	def __init__(self,response=None,url=None,fake=False,http=None):
		self._xpath = None
		self._json = None
		if fake:
			url = 'http://www.example.com/'
			self.original_url = url
			self.final_url = url
			self._domain = urlparse.urlparse(url).netloc
			self._data = '<html><body><p>Hello!</p></body></html>'
			self._encoded_data = self._data
		else:
			self._domain = urlparse.urlparse(url).netloc
			self.headers = response.info()
			compressed_data = response.read()
			if filter(lambda (k,v): k.lower() == 'content-encoding' and v.lower() == 'gzip', self.headers.items()):
				self.headers['Content-type'] = 'text/html; charset=utf-8'
				self._data = gzip.GzipFile(fileobj=StringIO.StringIO(compressed_data)).read()
			else:
				self._data = compressed_data
				
			self._encoded_data = unicode(self._data,'ISO-8859-1').encode('ISO-8859-1')
			
			self.original_url = url
			self.final_url = response.geturl()

		if http:
			self.http = http
		
	def __str__(self):
		return self._data
		
	def __len__(self):
		return len(str(self))

	def __contains__(self,x):
		return x.lower() in str(self).lower()
		
	def save(self,handle):
		if isinstance(handle,basestring):
			handle = open(handle,'w')
		handle.write(str(self))

	def json(self):
		if not self._json:
			self._json = json.loads(self._data)
		return self._json	
		
	def xpath(self,expression):
		if self._xpath is None:
			self._xpath = etree.HTML(self._encoded_data)
			if self._xpath is None:
				return []

		if not isinstance(expression,basestring):
			expression = '||'.join(expression)
		if '||' in expression:
			results = []
			for part in expression.split('||'):
				results.append(self.xpath(part))
			return zip(*results)

		results = []
		original_expression = expression
		if expression.endswith('/string()'):
			expression = expression.split('/string()')[0]
		xpath_result = self._xpath.xpath(expression)
		if isinstance(xpath_result,basestring) or not isinstance(xpath_result,collections.Iterable):
			return xpath_result
		for result in xpath_result:
			if expression.endswith('@href') or expression.endswith('@src'):
				if not result.startswith('http'):
					result = urlparse.urljoin(self.final_url,result)
				result = result.split('#')[0]
			if original_expression.endswith('/string()'):
				result = result.xpath('string()')
			if isinstance(result,basestring):
				result = result.strip()
			if isinstance(result,basestring):
				if len(result):
					results.append(result)
			else:
				results.append(result)
		return list(results)
				
		
	def single_xpath(self,expression):
		results = self.xpath(expression)
		if isinstance(results,basestring) or not isinstance(results,collections.Iterable):
			return results
		if results:
			return results[0]
		else:
			return ''
			
	def internal_links(self):
		return set([link for link in self.xpath('//a/@href') if urlparse.urlparse(link).netloc == self._domain])
		
	def external_links(self):
		return set([link for link in self.xpath('//a/@href') if urlparse.urlparse(link).netloc != self._domain and link.lower().startswith('http')])
		
	def dofollow_links(self):
		return set(self.xpath('//a[@rel!="nofollow" or not(@rel)]/@href'))
	
	def nofollow_links(self):
		return set(self.xpath('//a[@rel="nofollow"]/@href'))
		
	def external_images(self):
		return set([image for image in self.xpath('//img/@src') if urlparse.urlparse(image).netloc != self._domain])

	def csv(self):
		return csv.reader(self._encoded_data)

	def regex(self,expression):
		if not isinstance(expression,basestring):
			expression = '||'.join(expression)
		if '||' in expression:
			results = []
			for part in expression.split('||'):
				results.append(self.regex(part))
			return zip(*results)
		return re.compile(expression,re.S|re.I).findall(self._encoded_data)

	def url_regex(self,expression):
		if not isinstance(expression,basestring):
			expression = '||'.join(expression)
		if '||' in expression:
			results = []
			for part in expression.split('||'):
				results.append(self.xpath(part))
			return zip(*results)
		return re.compile(expression).findall(self.final_url)

	def pagination(self):
		def score_link(link):
			score = 0
			for fragment in ('page','p=','pg=','pgn=','start','index'):
				if fragment in link.lower():
					score += 100
					break
			score -= levenshtein_distance(link,self.final_url)
			return score

		links = self.internal_links()
		number_links = [link for link in links if re.compile('/\d+/?').search(link) is not None or re.compile('=\d+').search(link) is not None or re.compile('\d+\.').search(link) is not None]
		matches = collections.defaultdict(list)

		for link in number_links:
			matches[''.join(re.compile('([^0-9]+)').findall(link))].append(link)
		filtered_keys = filter(lambda k: len(matches[k]) > 1,matches.keys())
		best_key = max(filtered_keys,key=score_link)
		template_url = matches[best_key][0]

		fragment_counter = collections.defaultdict(set)

		for link in matches[best_key]:
			fragments = re.compile('([^\d]+)(\d+)').findall(link)
			for fragment, number in fragments:
				fragment_counter[fragment].add(int(number))
		fragment = max(fragment_counter.keys(),key=lambda k: len(fragment_counter[k]))
		print fragment, fragment_counter[fragment]
		results = [self.final_url]
		numbers = sorted(list(fragment_counter[fragment]))
		difference = numbers[1] - numbers[0]

		if numbers[-1] - numbers[0] > len(numbers) * difference:
			numbers = range(numbers[0],numbers[-1]+difference,difference)
		for number in numbers:
			results.append(re.sub(re.escape('%s' % fragment)+'\d+','%s%s' % (fragment,number),template_url))
		return results
		
	def __unicode__(self):
		return 'HTTPResponse for %s' % self.final_url
		
	def link_with_url(self,link,domain=False):
		if domain:
			link = urlparse.urlparse(link).netloc
		for l,l_obj in self.xpath('//a/@href||//a[@href]'):
			if domain:
				if urlparse.urlparse(l).netloc == link:
					return l_obj
			else:
				if link in (l,l+'/',l.rstrip('/')):
					return l_obj
		return False

	def link_with_anchor(self,anchor):
		results = self.xpath('//a[text()="%s"]' % anchor)
		if len(results):
			return results[0]
		return False

	def image_captcha(self,xpath):
		from captcha import DBC_USERNAME, DBC_PASSWORD
		image_source = self.single_xpath(xpath)
		if image_source:
			image = grab(image_source,http_obj=self.http)
			image.save('captcha.jpg')
			result = deathbycaptcha.HttpClient(DBC_USERNAME,DBC_PASSWORD).decode(StringIO.StringIO(str(image)))
			if result:
				return result['text']

	def recaptcha(self):
		iframe_source = self.single_xpath('//iframe[contains(@src,"recaptcha")]/@src')
		if iframe_source:
			iframe = grab(iframe_source,http_obj=self.http,ref=self.final_url)
			return (iframe.single_xpath('//input[@id="recaptcha_challenge_field"]/@value'),iframe.image_captcha('//center/img/@src'))

	def hidden_fields(self):
		fields = {}
		for name, value in self.xpath('//input[@type="hidden"]/@name||//input[@type="hidden"]/@value'):
			fields[name] = value
		return fields
		

class ProxyManager(object):
	def __init__(self,proxy=True,delay=60):
		if isinstance(proxy,list):
			proxies = proxy
		elif proxy == True:
			proxies = open('proxies.txt').read().strip().split('\n')
		elif os.path.isfile(proxy):
			proxies = [p.strip() for p in open(proxy) if len(p.strip())]
		elif ':' in proxy:
			proxies = proxy.strip().split('\n')
		else:
			proxies = [None]
			
		self.records = dict(zip(proxies,[0 for p in proxies]))
		self.delay = delay
		
	def get(self,debug=False):
		while True:
			proxies = [proxy for proxy,proxy_time in self.records.items() if proxy_time + self.delay < time.time()]
			if not proxies:
				gevent.sleep(1)
			else:
				if debug:
					print '%s Proxies available.' % len(proxies)
				proxy = random.sample(proxies,1)[0]
				self.records[proxy] = int(time.time())
				return proxy
		

class HeadRequest(urllib2.Request):
	def get_method(self):
		return 'HEAD'

def useragent():
	agents = ('Mozilla/5.0 (Windows; U; Windows NT 5.1; en-GB; rv:1.8.1.6) Gecko/20070725 Firefox/2.0.0.6','Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 5.1)','Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 5.1; .NET CLR 1.1.4322; .NET CLR 2.0.50727; .NET CLR 3.0.04506.30)','Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; .NET CLR 1.1.4322)','Mozilla/5.0 (X11; Arch Linux i686; rv:2.0) Gecko/20110321 Firefox/4.0','Mozilla/5.0 (Windows; U; Windows NT 6.1; ru; rv:1.9.2.3) Gecko/20100401 Firefox/4.0 (.NET CLR 3.5.30729)','Mozilla/5.0 (Windows NT 6.1; rv:2.0) Gecko/20110319 Firefox/4.0','Mozilla/5.0 (Windows NT 6.1; rv:1.9) Gecko/20100101 Firefox/4.0','Opera/9.20 (Windows NT 6.0; U; en)','Opera/9.00 (Windows NT 5.1; U; en)','Opera/9.64(Windows NT 5.1; U; en) Presto/2.1.1')
	return random.choice(agents)
	
def encode_multipart_formdata(fields, files):
	'''
	fields is a sequence of (name, value) elements for regular form fields.
	files is a sequence of (name, filename, value) elements for data to be uploaded as files
	Return (content_type, body) ready for httplib.HTTP instance
	'''
	BOUNDARY = '----------ThIs_Is_tHe_bouNdaRY_$'
	CRLF = '\r\n'
	L = []
	for (key, value) in fields:
		L.append('--' + BOUNDARY)
		L.append('Content-Disposition: form-data; name="%s"' % key)
		L.append('')
		L.append(value)
	for (key, filename, value) in files:
		L.append('--' + BOUNDARY)
		L.append('Content-Disposition: form-data; name="%s"; filename="%s"' % (key, filename))
		L.append('Content-Type: %s' % get_content_type(filename))
		L.append('')
		L.append(value)
	L.append('--' + BOUNDARY + '--')
	L.append('')
	body = CRLF.join(L)
	content_type = 'multipart/form-data; boundary=%s' % BOUNDARY
	return content_type, body

def get_content_type(filename):
	return mimetypes.guess_type(filename)[0] or 'application/octet-stream'
	
class http(object):
	def __init__(self,proxy=None,cookie_filename=None,cookies=True):
		self.handlers = set()
		try:
			useragents = open('useragents.txt').read().strip().split('\n')
			self.useragent = random.choice(useragents).strip()
		except:
			self.useragent = useragent()
			
		self.opener = urllib2.OpenerDirector()
		
		if cookies:
			self.cookie_jar = cookielib.LWPCookieJar()
			if cookie_filename:
				self.cookie_jar = cookielib.MozillaCookieJar(cookie_filename)
				self.cookie_jar.load()
			cookie_support = urllib2.HTTPCookieProcessor(self.cookie_jar)
		else:
			cookie_support = None
			
		self.proxy = False
		proxy_auth = None
		
		if proxy:
			if isinstance(proxy,ProxyManager):
				self.proxy = proxy.get()
			else:
				self.proxy = ProxyManager(proxy).get()

		if self.proxy:
			self.proxy = self.proxy.strip()
			proxy_support = urllib2.ProxyHandler({'http' : self.proxy,'https':self.proxy})
			if '@' in self.proxy:
				proxy_auth = urllib2.HTTPBasicAuthHandler()
			else:
				proxy_auth = None
		else:
			proxy_support = None
		self.build_opener(proxy_support,cookie_support,proxy_auth)
			
	def build_opener(self,*handlers):
		self.handlers |= set([handler for handler in handlers if handler is not None])
		self.opener = urllib2.build_opener(*self.handlers)

	def urlopen(self,url,post=None,ref='',files=None,username=None,password=None,compress=True,head=False,timeout=30):
		assert url.lower().startswith('http')
		if isinstance(post,basestring):
			post = dict([part.split('=') for part in post.strip().split('&')])
			print post
		if username and password:
			password_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
			password_manager.add_password(None,url,username,password)
			password_auth = urllib2.HTTPBasicAuthHandler(password_manager)
			self.build_opener(password_auth)
		urllib2.install_opener(self.opener)
		if compress:
			headers = {'User-Agent' : self.useragent, 'Referer' : ref, 'Accept-encoding' : 'gzip'}
		else:
			headers = {'User-Agent' : self.useragent, 'Referer' : ref}
		if files:
			content_type,post = encode_multipart_formdata(post.items(), files)
			headers['content-type'] = content_type
			headers['content-length'] = str(len(post))
		elif post:
			post = urllib.urlencode(post)
		if head:
			req = HeadRequest(url,post,headers)
		else:
			req = urllib2.Request(url,post,headers)
		with gevent.Timeout(timeout):
			response = urllib2.urlopen(req)
			return HTTPResponse(response,url,http=self)
		
def grab(url,proxy=None,post=None,ref=None,compress=True,include_url=False,retries=5,http_obj=None,cookies=False):
	data = None
	for i in range(retries):
		if not http_obj:
			http_obj = http(proxy,cookies=cookies)
		try:
			data = http_obj.urlopen(url=url,post=post,ref=ref,compress=compress)
			break
		except:
			pass
	if data:
		return data
	return False
   	 
def multi_grab(urls,proxy=None,ref=None,compress=True,delay=10,pool_size=10,retries=5,http_obj=None,queue_links=UberIterator()):
	if proxy is not None:
		proxy = web.ProxyManager(proxy,delay=delay)
		pool_size = len(proxy.records)
	work_pool = custompool.Pool(pool_size)
	partial_grab = partial(grab,proxy=proxy,post=None,ref=ref,compress=compress,include_url=True,retries=retries,http_obj=http_obj)
	if isinstance(urls,basestring):
		if '\n' in urls:
			urls = [url.strip() for url in urls.split('\n') if len(url.strip())]
		else:
			urls = [urls]
	queue_links += urls
	try:
		for result in work_pool.imap_unordered(partial_grab,queue_links):
			if result:
				if result.final_url.startswith('http'):
					yield result
	except:
		pass
		
def domain_grab(urls,http_obj=None,pool_size=10,retries=5,proxy=None,delay=10,debug=True,queue_links=UberIterator()):
	if isinstance(urls,basestring):
		if '\n' in urls:
			urls = [url.strip() for url in urls.split('\n') if len(url.strip())]
		else:
			urls = [urls]
	domains = set([urlparse.urlparse(url).netloc for url in urls])
	queue_links += urls
	seen_links = pybloom.ScalableBloomFilter(initial_capacity=100, error_rate=0.001, mode=pybloom.ScalableBloomFilter.SMALL_SET_GROWTH)
	seen_links.add([url for url in urls])
	while queue_links:
		if debug:
			progress_counter = 0
			progress_total = len(queue_links)
		for page in multi_grab(queue_links,http_obj=http_obj,pool_size=pool_size,retries=retries,proxy=proxy,delay=delay):
			if debug:
				progress_counter += 1
				print 'Got %s, Link %s/%s (%s%%)' % (page.final_url,progress_counter,progress_total,int((float(progress_counter)/progress_total)*100))
			if urlparse.urlparse(page.final_url).netloc in domains:
				yield page
				new_links = set([link for link in page.internal_links() if link not in seen_links and link.lower().split('.')[-1] not in ('jpg','gif','jpeg','pdf','doc','docx','ppt','txt')])
				queue_links += list(new_links)
				[seen_links.add(link) for link in new_links]
		if debug:
			print 'Seen Links: %s' %  len(seen_links)
			print 'Bloom Capacity: %s' % seen_links.capacity
			print 'Links in Queue: %s' % len(queue_links)
		

def redirecturl(url,proxy=None):
	return http(proxy).urlopen(url,head=True).geturl()
	
if __name__ == '__main__':
	for page in domain_grab(['http://www.bbc.co.uk/','http://www.reddit.com/','http://www.arstechnica.com/'],debug=True,pool_size=100):
		print page.final_url
		print 'Seen Links: %s' %  len(seen_links)
		print 'Bloom Capacity: %s' % seen_links.capacity
		print 'Links in Queue: %s' % len(queue_links)
		

def redirecturl(url,proxy=None):
	return http(proxy).urlopen(url,head=True).geturl()
	
if __name__ == '__main__':
	for page in domain_grab(['http://www.bbc.co.uk/','http://www.reddit.com/','http://www.arstechnica.com/'],debug=True,pool_size=100):
		print page.final_url
