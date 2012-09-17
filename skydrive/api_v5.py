#-*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function


import itertools as it, operator as op, functools as ft
from datetime import datetime, timedelta
import os, sys, math, urllib, urlparse, json, types
import requests

from .conf import ConfigMixin

import logging
log = logging.getLogger(__name__)



class SkyDriveInteractionError(Exception): pass

class ProtocolError(SkyDriveInteractionError):
	def __init__(self, msg, code=None):
		super(ProtocolError, self).__init__(msg)
		self.code = code

class AuthenticationError(SkyDriveInteractionError): pass



def request( url, method='get',
		data=None, files=None, raw=False, raise_for=dict() ):
	kwz, func = dict(), getattr(requests, method.lower())
	if data is not None: kwz['data'] = data
	if files is not None: kwz['files'] = files
	try:
		res = func(url, **kwz)
		code = res.status_code
		if code != requests.codes.ok: res.raise_for_status()
		return json.loads(res.text) if not raw else res.raw.read()
	except requests.RequestException as err:
		raise raise_for.get(code, ProtocolError)(err.message)

def urandom_hex(n):
	return os.urandom(int(math.ceil(n / 2.0))).encode('hex')[:n]



class SkyDriveAuth(object):

	# Client id/secret should be static on per-application basis.
	# Can be received from LiveConnect by any registered user at https://manage.dev.live.com/
	# API ToS can be found here at http://msdn.microsoft.com/en-US/library/live/ff765012
	client_id = client_secret = None

	auth_url_user = 'https://login.live.com/oauth20_authorize.srf'
	auth_url_token = 'https://login.live.com/oauth20_token.srf'
	auth_scope = 'wl.skydrive', 'wl.skydrive_update', 'wl.offline_access'
	auth_redirect_uri_mobile = 'https://login.live.com/oauth20_desktop.srf'

	# Set by auth_get_token, not used internally
	auth_access_expires = auth_access_data_raw = None

	# At least one of these should be set before data requests.
	auth_code = auth_refresh_token = auth_access_token = None

	# This (default) redirect_uri is **special** - app must be marked as "mobile" to use it.
	auth_redirect_uri = auth_redirect_uri_mobile


	def __init__(self, client_id=None, client_secret=None, urandom_len=16, **config):
		self.client_id = client_id or urandom_hex(urandom_len)
		self.client_secret = client_secret or urandom_hex(urandom_len)

		for k, v in config.viewitems():
			try: getattr(self, k)
			except AttributeError:
				raise AttributeError('Unrecognized configuration key: {}'.format(k))
			setattr(self, k, v)


	def auth_user_get_url(self, scope=None):
		# Note: default redirect_uri is **special**, app must be marked as "mobile" to use it
		return '{}?{}'.format( self.auth_url_user, urllib.urlencode(dict(
			client_id=self.client_id, scope=' '.join(scope or self.auth_scope),
			response_type='code', redirect_uri=self.auth_redirect_uri )) )

	def auth_user_process_url(self, url):
		url = urlparse.urlparse(url)
		url_qs = dict(it.chain.from_iterable(
			urlparse.parse_qsl(v) for v in [url.query, url.fragment] ))
		if url_qs.get('error'):
			raise AuthenticationError('{} :: {}'.format(
				url_qs['error'], url_qs.get('error_description') ))
		self.auth_code = url_qs['code']
		return self.auth_code


	def auth_get_token( self, check_scope=True,
			_check_keys=['client_id', 'client_secret', 'code', 'refresh_token', 'grant_type'] ):

		post_data = dict( client_id=self.client_id,
			client_secret=self.client_secret, redirect_uri=self.auth_redirect_uri )
		if not self.auth_refresh_token:
			log.debug('Requesting new access_token through authorization_code grant')
			post_data.update(code=self.auth_code, grant_type='authorization_code')
		else:
			if self.auth_redirect_uri == self.auth_redirect_uri_mobile:
				del post_data['client_secret'] # not necessary for "mobile" apps
			log.debug('Refreshing access_token')
			post_data.update(
				refresh_token=self.auth_refresh_token, grant_type='refresh_token' )
		post_data_missing_keys = list( k for k in
			_check_keys if k in post_data and not post_data[k] )
		if post_data_missing_keys:
			raise AuthenticationError( 'Insufficient authentication'
				' data provided (missing keys: {})'.format(post_data_missing_keys) )

		res = self.auth_access_data_raw =\
			request(self.auth_url_token, method='post', data=post_data)

		assert res['token_type'] == 'bearer'
		for k in 'access_token', 'refresh_token':
			if k in res: setattr(self, 'auth_{}'.format(k), res[k])
		self.auth_access_expires = None if 'expires_in' not in res\
			else (datetime.utcnow() + timedelta(0, res['expires_in']))

		scope_granted = res.get('scope', '').split()
		if check_scope and set(self.auth_scope) != set(scope_granted):
			raise AuthenticationError(
				"Granted scope ({}) doesn't match requested one ({})."\
				.format(', '.join(scope_granted), ', '.join(self.auth_scope)) )
		return scope_granted



class SkyDriveAPI(SkyDriveAuth):

	api_url_base = 'https://apis.live.net/v5.0/'

	# Return objects instead of ids by default from
	#  methods where such parameter can be passed.
	return_objects = False

	def _api_url( self, path, query=dict(),
			pass_access_token=True, pass_empty_values=False ):
		query = query.copy()
		if pass_access_token:
			query.setdefault('access_token', self.auth_access_token)
		if not pass_empty_values:
			for k,v in query.viewitems():
				if not v: raise ProtocolError('Empty key {!r} for API call (path: {})'.format(k, path))
		return urlparse.urljoin( self.api_url_base,
			'{}?{}'.format(path, urllib.urlencode(query)) )

	def __call__( self, url='me/skydrive', query=dict(),
			query_filter=True, auto_refresh_token=True, **request_kwz ):
		if query_filter:
			query = dict( (k,v) for k,v in
				query.viewitems() if v is not None )
		kwz = request_kwz.copy()
		kwz.setdefault('raise_for', dict())[401] = AuthenticationError
		try: return request(self._api_url(url, query), **kwz)
		except AuthenticationError:
			if not auto_refresh_token: raise
			self.auth_get_token()
			return request(self._api_url(url, query), **request_kwz)


	def get_quota(self):
		'Return tuple of bytes_available, bytes_quota.'
		return op.itemgetter('available', 'quota')(self('me/skydrive/quota'))

	def listdir( self, folder_id='me/skydrive',
			type_filter=None, limit=None, objects=None ):
		'''Return a dict ({name: id, ...}) of objects in the specified folder_id.
			type_filter can be set to type (str) or sequence of object types to return.
			limit is passed to the API, so might be used as optimization.
			objects flag allows to return a list of raw metadata objects
				(with type, links, description, timestamps) instead of just ids.'''
		if objects is None: objects = self.return_objects
		lst = self(os.path.join(folder_id, 'files'), dict(limit=limit)).get('data', list())
		if type_filter:
			if isinstance(type_filter, types.StringTypes): type_filter = {type_filter}
			lst = list(obj for obj in lst if obj['type'] in type_filter)
		lst = dict(it.imap(op.itemgetter('name', 'id'), lst))\
			if not objects else dict((obj['name'], obj) for obj in lst)
		return lst

	def get(self, obj_id='me/skydrive'):
		'Return metadata of a specified object.'
		return self(obj_id)

	def get_by_path(self, path, root_id='me/skydrive', objects=None):
		'''Return id (or metadata) of an object, specified by chain
				(iterable or fs-style path string) of "name" attributes of it's ancestors.
			Requires a lot of calls to resolve each name in path, so use with care.
			root_id parameter allows to specify path
				 relative to some folder_id (default: me/skydrive).'''
		if objects is None: objects = self.return_objects
		if isinstance(path, types.StringTypes):
			path = filter(None, path.split(os.sep))
		for name in path: root_id = self.listdir(root_id)[name]
		return root_id if not objects else self.get(root_id)



class PersistentSkyDriveAPI(SkyDriveAPI, ConfigMixin):

	@ft.wraps(SkyDriveAPI.auth_get_token)
	def auth_get_token(self, *argz, **kwz):
		# Wrapped to push new tokens to storage asap.
		ret = super(PersistentSkyDriveAPI, self).auth_get_token(*argz, **kwz)
		self.sync()
		return ret

	def __del__(self): self.sync()