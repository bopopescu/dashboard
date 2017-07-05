# Copyright 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Token provider interface."""

import abc
import base64
import copy
import datetime
import sys
import uuid

from oslo_log import log
from oslo_utils import timeutils
import six

from keystone.common import cache
from keystone.common import dependency
from keystone.common import manager
import keystone.conf
from keystone import exception
from keystone.i18n import _, _LE
from keystone.models import token_model
from keystone import notifications
from keystone.token import persistence
from keystone.token import providers
from keystone.token import utils


CONF = keystone.conf.CONF
LOG = log.getLogger(__name__)

TOKENS_REGION = cache.create_region(name='tokens')
MEMOIZE_TOKENS = cache.get_memoization_decorator(
    group='token',
    region=TOKENS_REGION)

# NOTE(morganfainberg): This is for compatibility in case someone was relying
# on the old location of the UnsupportedTokenVersionException for their code.
UnsupportedTokenVersionException = exception.UnsupportedTokenVersionException

# supported token versions
V2 = token_model.V2
V3 = token_model.V3
VERSIONS = token_model.VERSIONS


def base64_encode(s):
    """Encode a URL-safe string.

    :type s: six.text_type
    :rtype: six.text_type

    """
    # urlsafe_b64encode() returns six.binary_type so need to convert to
    # six.text_type, might as well do it before stripping.
    return base64.urlsafe_b64encode(s).decode('utf-8').rstrip('=')


def random_urlsafe_str():
    """Generate a random URL-safe string.

    :rtype: six.text_type

    """
    # chop the padding (==) off the end of the encoding to save space
    return base64.urlsafe_b64encode(uuid.uuid4().bytes)[:-2].decode('utf-8')


def random_urlsafe_str_to_bytes(s):
    """Convert a string from :func:`random_urlsafe_str()` to six.binary_type.

    :type s: six.text_type
    :rtype: six.binary_type

    """
    # urlsafe_b64decode() requires str, unicode isn't accepted.
    s = str(s)

    # restore the padding (==) at the end of the string
    return base64.urlsafe_b64decode(s + '==')


def default_expire_time():
    """Determine when a fresh token should expire.

    Expiration time varies based on configuration (see ``[token] expiration``).

    :returns: a naive UTC datetime.datetime object

    """
    expire_delta = datetime.timedelta(seconds=CONF.token.expiration)
    expires_at = timeutils.utcnow() + expire_delta
    return expires_at.replace(microsecond=0)


def audit_info(parent_audit_id):
    """Build the audit data for a token.

    If ``parent_audit_id`` is None, the list will be one element in length
    containing a newly generated audit_id.

    If ``parent_audit_id`` is supplied, the list will be two elements in length
    containing a newly generated audit_id and the ``parent_audit_id``. The
    ``parent_audit_id`` will always be element index 1 in the resulting
    list.

    :param parent_audit_id: the audit of the original token in the chain
    :type parent_audit_id: str
    :returns: Keystone token audit data
    """
    audit_id = random_urlsafe_str()
    if parent_audit_id is not None:
        return [audit_id, parent_audit_id]
    return [audit_id]


@dependency.provider('token_provider_api')
@dependency.requires('assignment_api', 'revoke_api')
class Manager(manager.Manager):
    """Default pivot point for the token provider backend.

    See :mod:`keystone.common.manager.Manager` for more details on how this
    dynamically calls the backend.

    """

    driver_namespace = 'keystone.token.provider'

    V2 = V2
    V3 = V3
    VERSIONS = VERSIONS
    INVALIDATE_PROJECT_TOKEN_PERSISTENCE = 'invalidate_project_tokens'
    INVALIDATE_USER_TOKEN_PERSISTENCE = 'invalidate_user_tokens'
    _persistence_manager = None

    def __init__(self):
        super(Manager, self).__init__(CONF.token.provider)
        self._register_callback_listeners()

    def _register_callback_listeners(self):
        # This is used by the @dependency.provider decorator to register the
        # provider (token_provider_api) manager to listen for trust deletions.
        callbacks = {
            notifications.ACTIONS.deleted: [
                ['OS-TRUST:trust', self._trust_deleted_event_callback],
                ['user', self._delete_user_tokens_callback],
                ['domain', self._delete_domain_tokens_callback],
            ],
            notifications.ACTIONS.disabled: [
                ['user', self._delete_user_tokens_callback],
                ['domain', self._delete_domain_tokens_callback],
                ['project', self._delete_project_tokens_callback],
            ],
            notifications.ACTIONS.internal: [
                [notifications.INVALIDATE_USER_TOKEN_PERSISTENCE,
                    self._delete_user_tokens_callback],
                [notifications.INVALIDATE_USER_PROJECT_TOKEN_PERSISTENCE,
                    self._delete_user_project_tokens_callback],
                [notifications.INVALIDATE_USER_OAUTH_CONSUMER_TOKENS,
                    self._delete_user_oauth_consumer_tokens_callback],
            ]
        }

        for event, cb_info in callbacks.items():
            for resource_type, callback_fns in cb_info:
                notifications.register_event_callback(event, resource_type,
                                                      callback_fns)

    @property
    def _needs_persistence(self):
        return self.driver.needs_persistence()

    @property
    def _persistence(self):
        # NOTE(morganfainberg): This should not be handled via __init__ to
        # avoid dependency injection oddities circular dependencies (where
        # the provider manager requires the token persistence manager, which
        # requires the token provider manager).
        if self._persistence_manager is None:
            self._persistence_manager = persistence.PersistenceManager()
        return self._persistence_manager

    def _create_token(self, token_id, token_data):
        try:
            if isinstance(token_data['expires'], six.string_types):
                token_data['expires'] = timeutils.normalize_time(
                    timeutils.parse_isotime(token_data['expires']))
            self._persistence.create_token(token_id, token_data)
        except Exception:
            exc_info = sys.exc_info()
            # an identical token may have been created already.
            # if so, return the token_data as it is also identical
            try:
                self._persistence.get_token(token_id)
            except exception.TokenNotFound:
                six.reraise(*exc_info)

    def validate_token(self, token_id, belongs_to=None):
        unique_id = utils.generate_unique_id(token_id)
        # NOTE(morganfainberg): Ensure we never use the long-form token_id
        # (PKI) as part of the cache_key.
        token = self._validate_token(unique_id)
        self._token_belongs_to(token, belongs_to)
        self._is_valid_token(token)
        return token

    def check_revocation_v2(self, token):
        try:
            token_data = token['access']
        except KeyError:
            raise exception.TokenNotFound(_('Failed to validate token'))

        token_values = self.revoke_api.model.build_token_values_v2(
            token_data, CONF.identity.default_domain_id)
        self.revoke_api.check_token(token_values)

    def validate_v2_token(self, token_id, belongs_to=None):
        # NOTE(lbragstad): Only go to the persistence backend if the token
        # provider requires it.
        if self._needs_persistence:
            # NOTE(morganfainberg): Ensure we never use the long-form token_id
            # (PKI) as part of the cache_key.
            unique_id = utils.generate_unique_id(token_id)
            token_ref = self._persistence.get_token(unique_id)
            token = self._validate_v2_token(token_ref)
        else:
            # NOTE(lbragstad): If the token doesn't require persistence, then
            # it is a fernet token. The fernet token provider doesn't care if
            # it's creating version 2.0 tokens or v3 tokens, so we use the same
            # validate_non_persistent_token() method to validate both. Then we
            # can leverage a separate method to make version 3 token data look
            # like version 2.0 token data. The pattern we want to move towards
            # is one where the token providers just handle data and the
            # controller layers handle interpreting the token data in a format
            # that makes sense for the request.
            v3_token_ref = self.validate_non_persistent_token(token_id)
            v2_token_data_helper = providers.common.V2TokenDataHelper()
            token = v2_token_data_helper.v3_to_v2_token(v3_token_ref, token_id)

        # these are common things that happen regardless of token provider
        self._token_belongs_to(token, belongs_to)
        self._is_valid_token(token)
        return token

    def check_revocation_v3(self, token):
        try:
            token_data = token['token']
        except KeyError:
            raise exception.TokenNotFound(_('Failed to validate token'))
        token_values = self.revoke_api.model.build_token_values(token_data)
        self.revoke_api.check_token(token_values)

    def check_revocation(self, token):
        version = self.get_token_version(token)
        if version == V2:
            return self.check_revocation_v2(token)
        else:
            return self.check_revocation_v3(token)

    def validate_v3_token(self, token_id):
        if not token_id:
            raise exception.TokenNotFound(_('No token in the request'))

        try:
            # NOTE(lbragstad): Only go to persistent storage if we have a token
            # to fetch from the backend (the driver persists the token).
            # Otherwise the information about the token must be in the token
            # id.
            if not self._needs_persistence:
                token_ref = self.validate_non_persistent_token(token_id)
            else:
                unique_id = utils.generate_unique_id(token_id)
                # NOTE(morganfainberg): Ensure we never use the long-form
                # token_id (PKI) as part of the cache_key.
                token_ref = self._persistence.get_token(unique_id)
                token_ref = self._validate_v3_token(token_ref)
            self._is_valid_token(token_ref)
            return token_ref
        except exception.Unauthorized as e:
            LOG.debug('Unable to validate token: %s', e)
            raise exception.TokenNotFound(token_id=token_id)

    @MEMOIZE_TOKENS
    def validate_non_persistent_token(self, token_id):
        return self.driver.validate_non_persistent_token(token_id)

    @MEMOIZE_TOKENS
    def _validate_token(self, token_id):
        if not token_id:
            raise exception.TokenNotFound(_('No token in the request'))

        try:
            if not self._needs_persistence:
                # NOTE(lbragstad): This will validate v2 and v3 non-persistent
                # tokens.
                return self.driver.validate_non_persistent_token(token_id)
            token_ref = self._persistence.get_token(token_id)
            version = self.get_token_version(token_ref)
            if version == self.V3:
                return self.driver.validate_v3_token(token_ref)
        except exception.Unauthorized as e:
            LOG.debug('Unable to validate token: %s', e)
            raise exception.TokenNotFound(token_id=token_id)
        if version == self.V2:
            return self.driver.validate_v2_token(token_ref)
        raise exception.UnsupportedTokenVersionException()

    @MEMOIZE_TOKENS
    def _validate_v2_token(self, token_id):
        return self.driver.validate_v2_token(token_id)

    @MEMOIZE_TOKENS
    def _validate_v3_token(self, token_id):
        return self.driver.validate_v3_token(token_id)

    def _is_valid_token(self, token):
        """Verify the token is valid format and has not expired."""
        current_time = timeutils.normalize_time(timeutils.utcnow())

        try:
            # Get the data we need from the correct location (V2 and V3 tokens
            # differ in structure, Try V3 first, fall back to V2 second)
            token_data = token.get('token', token.get('access'))
            expires_at = token_data.get('expires_at',
                                        token_data.get('expires'))
            if not expires_at:
                expires_at = token_data['token']['expires']
            expiry = timeutils.normalize_time(
                timeutils.parse_isotime(expires_at))
        except Exception:
            LOG.exception(_LE('Unexpected error or malformed token '
                              'determining token expiry: %s'), token)
            raise exception.TokenNotFound(_('Failed to validate token'))

        if current_time < expiry:
            self.check_revocation(token)
            # Token has not expired and has not been revoked.
            return None
        else:
            raise exception.TokenNotFound(_('Failed to validate token'))

    def _token_belongs_to(self, token, belongs_to):
        """Check if the token belongs to the right tenant.

        This is only used on v2 tokens.  The structural validity of the token
        will have already been checked before this method is called.

        """
        if belongs_to:
            token_data = token['access']['token']
            if ('tenant' not in token_data or
                    token_data['tenant']['id'] != belongs_to):
                raise exception.Unauthorized()

    def issue_v2_token(self, token_ref, roles_ref=None, catalog_ref=None):
        token_id, token_data = self.driver.issue_v2_token(
            token_ref, roles_ref, catalog_ref)

        if self._needs_persistence:
            data = dict(key=token_id,
                        id=token_id,
                        expires=token_data['access']['token']['expires'],
                        user=token_ref['user'],
                        tenant=token_ref['tenant'],
                        metadata=token_ref['metadata'],
                        token_data=token_data,
                        bind=token_ref.get('bind'),
                        trust_id=token_ref['metadata'].get('trust_id'),
                        token_version=self.V2)
            self._create_token(token_id, data)

        # NOTE(amakarov): TOKENS_REGION is to be passed to serve as
        # required positional "self" argument. It's ignored, so I've put
        # it here for convenience - any placeholder is fine.
        # NOTE(amakarov): v3 token data can be converted to v2.0 version,
        # so v2.0 token validation cache can also be populated. However it
        # isn't reflexive: there is no way to populate v3 validation cache
        # on issuing a token using v2.0 API.
        if CONF.token.cache_on_issue:
            self._validate_v2_token.set(token_data, TOKENS_REGION, token_id)

        return token_id, token_data

    def issue_v3_token(self, user_id, method_names, expires_at=None,
                       project_id=None, is_domain=False, domain_id=None,
                       auth_context=None, trust=None, metadata_ref=None,
                       include_catalog=True, parent_audit_id=None):
        token_id, token_data = self.driver.issue_v3_token(
            user_id, method_names, expires_at, project_id, domain_id,
            auth_context, trust, metadata_ref, include_catalog,
            parent_audit_id)

        if metadata_ref is None:
            metadata_ref = {}

        if 'project' in token_data['token']:
            # project-scoped token, fill in the v2 token data
            # all we care are the role IDs

            # FIXME(gyee): is there really a need to store roles in metadata?
            role_ids = [r['id'] for r in token_data['token']['roles']]
            metadata_ref = {'roles': role_ids}
            is_domain = token_data['token']['is_domain']

        if trust:
            metadata_ref.setdefault('trust_id', trust['id'])
            metadata_ref.setdefault('trustee_user_id',
                                    trust['trustee_user_id'])

        data = dict(key=token_id,
                    id=token_id,
                    expires=token_data['token']['expires_at'],
                    user=token_data['token']['user'],
                    tenant=token_data['token'].get('project'),
                    is_domain=is_domain,
                    metadata=metadata_ref,
                    token_data=token_data,
                    trust_id=trust['id'] if trust else None,
                    token_version=self.V3)
        if self._needs_persistence:
            self._create_token(token_id, data)

        if CONF.token.cache_on_issue:
            # NOTE(amakarov): here and above TOKENS_REGION is to be passed
            # to serve as required positional "self" argument. It's ignored,
            # so I've put it here for convenience - any placeholder is fine.
            self._validate_v3_token.set(token_data, TOKENS_REGION, token_id)
            self._validate_token.set(token_data, TOKENS_REGION, token_id)
            self.validate_non_persistent_token.set(
                token_data, TOKENS_REGION, token_id)

            try:
                v2_helper = providers.common.V2TokenDataHelper()
                v2_token_data = v2_helper.v3_to_v2_token(
                    copy.deepcopy(token_data), token_id)
            except exception.Unauthorized:
                # Ignore trust and oauth tokens
                pass
            else:
                self._validate_v2_token.set(
                    v2_token_data, TOKENS_REGION, token_id)

        return token_id, token_data

    def invalidate_individual_token_cache(self, token_id):
        # NOTE(morganfainberg): invalidate takes the exact same arguments as
        # the normal method, this means we need to pass "self" in (which gets
        # stripped off).

        # FIXME(morganfainberg): Does this cache actually need to be
        # invalidated? We maintain a cached revocation list, which should be
        # consulted before accepting a token as valid.  For now we will
        # do the explicit individual token invalidation.

        self._validate_token.invalidate(self, token_id)
        self._validate_v2_token.invalidate(self, token_id)
        self._validate_v3_token.invalidate(self, token_id)
        # This method isn't actually called in the case of non-persistent
        # tokens, but we include the invalidation in case this ever changes
        # in the future.
        self.validate_non_persistent_token.invalidate(self, token_id)

    def revoke_token(self, token_id, revoke_chain=False):
        token_ref = token_model.KeystoneToken(
            token_id=token_id,
            token_data=self.validate_token(token_id))

        project_id = token_ref.project_id if token_ref.project_scoped else None
        domain_id = token_ref.domain_id if token_ref.domain_scoped else None

        if revoke_chain:
            self.revoke_api.revoke_by_audit_chain_id(token_ref.audit_chain_id,
                                                     project_id=project_id,
                                                     domain_id=domain_id)
        else:
            self.revoke_api.revoke_by_audit_id(token_ref.audit_id)

        if CONF.token.revoke_by_id and self._needs_persistence:
            self._persistence.delete_token(token_id=token_id)

    def list_revoked_tokens(self):
        return self._persistence.list_revoked_tokens()

    def _trust_deleted_event_callback(self, service, resource_type, operation,
                                      payload):
        if CONF.token.revoke_by_id:
            trust_id = payload['resource_info']
            trust = self.trust_api.get_trust(trust_id, deleted=True)
            self._persistence.delete_tokens(user_id=trust['trustor_user_id'],
                                            trust_id=trust_id)
        if CONF.token.cache_on_issue:
            # NOTE(amakarov): preserving behavior
            TOKENS_REGION.invalidate()

    def _delete_user_tokens_callback(self, service, resource_type, operation,
                                     payload):
        if CONF.token.revoke_by_id:
            user_id = payload['resource_info']
            self._persistence.delete_tokens_for_user(user_id)
        if CONF.token.cache_on_issue:
            # NOTE(amakarov): preserving behavior
            TOKENS_REGION.invalidate()

    def _delete_domain_tokens_callback(self, service, resource_type,
                                       operation, payload):
        if CONF.token.revoke_by_id:
            domain_id = payload['resource_info']
            self._persistence.delete_tokens_for_domain(domain_id=domain_id)
        if CONF.token.cache_on_issue:
            # NOTE(amakarov): preserving behavior
            TOKENS_REGION.invalidate()

    def _delete_user_project_tokens_callback(self, service, resource_type,
                                             operation, payload):
        if CONF.token.revoke_by_id:
            user_id = payload['resource_info']['user_id']
            project_id = payload['resource_info']['project_id']
            self._persistence.delete_tokens_for_user(user_id=user_id,
                                                     project_id=project_id)
        if CONF.token.cache_on_issue:
            # NOTE(amakarov): preserving behavior
            TOKENS_REGION.invalidate()

    def _delete_project_tokens_callback(self, service, resource_type,
                                        operation, payload):
        if CONF.token.revoke_by_id:
            project_id = payload['resource_info']
            self._persistence.delete_tokens_for_users(
                self.assignment_api.list_user_ids_for_project(project_id),
                project_id=project_id)
        if CONF.token.cache_on_issue:
            # NOTE(amakarov): preserving behavior
            TOKENS_REGION.invalidate()

    def _delete_user_oauth_consumer_tokens_callback(self, service,
                                                    resource_type, operation,
                                                    payload):
        if CONF.token.revoke_by_id:
            user_id = payload['resource_info']['user_id']
            consumer_id = payload['resource_info']['consumer_id']
            self._persistence.delete_tokens(user_id=user_id,
                                            consumer_id=consumer_id)
        if CONF.token.cache_on_issue:
            # NOTE(amakarov): preserving behavior
            TOKENS_REGION.invalidate()


@six.add_metaclass(abc.ABCMeta)
class Provider(object):
    """Interface description for a Token provider."""

    @abc.abstractmethod
    def needs_persistence(self):
        """Determine if the token should be persisted.

        If the token provider requires that the token be persisted to a
        backend this should return True, otherwise return False.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_token_version(self, token_data):
        """Return the version of the given token data.

        If the given token data is unrecognizable,
        UnsupportedTokenVersionException is raised.

        :param token_data: token_data
        :type token_data: dict
        :returns: token version string
        :raises keystone.exception.UnsupportedTokenVersionException:
            If the token version is not expected.
        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def issue_v2_token(self, token_ref, roles_ref=None, catalog_ref=None):
        """Issue a V2 token.

        :param token_ref: token data to generate token from
        :type token_ref: dict
        :param roles_ref: optional roles list
        :type roles_ref: dict
        :param catalog_ref: optional catalog information
        :type catalog_ref: dict
        :returns: (token_id, token_data)
        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def issue_v3_token(self, user_id, method_names, expires_at=None,
                       project_id=None, domain_id=None, auth_context=None,
                       trust=None, metadata_ref=None, include_catalog=True,
                       parent_audit_id=None):
        """Issue a V3 Token.

        :param user_id: identity of the user
        :type user_id: string
        :param method_names: names of authentication methods
        :type method_names: list
        :param expires_at: optional time the token will expire
        :type expires_at: string
        :param project_id: optional project identity
        :type project_id: string
        :param domain_id: optional domain identity
        :type domain_id: string
        :param auth_context: optional context from the authorization plugins
        :type auth_context: dict
        :param trust: optional trust reference
        :type trust: dict
        :param metadata_ref: optional metadata reference
        :type metadata_ref: dict
        :param include_catalog: optional, include the catalog in token data
        :type include_catalog: boolean
        :param parent_audit_id: optional, the audit id of the parent token
        :type parent_audit_id: string
        :returns: (token_id, token_data)
        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def validate_v2_token(self, token_ref):
        """Validate the given V2 token and return the token data.

        Must raise Unauthorized exception if unable to validate token.

        :param token_ref: the token reference
        :type token_ref: dict
        :returns: token data
        :raises keystone.exception.TokenNotFound: If the token doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def validate_non_persistent_token(self, token_id):
        """Validate a given non-persistent token id and return the token_data.

        :param token_id: the token id
        :type token_id: string
        :returns: token data
        :raises keystone.exception.TokenNotFound: When the token is invalid
        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def validate_v3_token(self, token_ref):
        """Validate the given V3 token and return the token_data.

        :param token_ref: the token reference
        :type token_ref: dict
        :returns: token data
        :raises keystone.exception.TokenNotFound: If the token doesn't exist.
        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def _get_token_id(self, token_data):
        """Generate the token_id based upon the data in token_data.

        :param token_data: token information
        :type token_data: dict
        :returns: token identifier
        :rtype: six.text_type
        """
        raise exception.NotImplemented()  # pragma: no cover