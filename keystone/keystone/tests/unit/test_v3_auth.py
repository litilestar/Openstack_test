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

import copy
import datetime
import itertools
import operator
import re
import uuid

import freezegun
from keystoneclient.common import cms
import mock
from oslo_serialization import jsonutils as json
from oslo_utils import fixture
from oslo_utils import timeutils
import six
from six.moves import http_client
from six.moves import range
from testtools import matchers
from testtools import testcase

from keystone import auth
from keystone.auth.plugins import totp
from keystone.common import utils
import keystone.conf
from keystone.credential.providers import fernet as credential_fernet
from keystone import exception
from keystone.identity.backends import resource_options as ro
from keystone.policy.backends import rules
from keystone.tests.common import auth as common_auth
from keystone.tests import unit
from keystone.tests.unit import ksfixtures
from keystone.tests.unit import test_v3


CONF = keystone.conf.CONF


class TestMFARules(test_v3.RestfulTestCase):
    def config_overrides(self):
        super(TestMFARules, self).config_overrides()
        self.useFixture(
            ksfixtures.KeyRepository(
                self.config_fixture,
                'fernet_tokens',
                CONF.fernet_tokens.max_active_keys
            )
        )

        self.useFixture(
            ksfixtures.KeyRepository(
                self.config_fixture,
                'credential',
                credential_fernet.MAX_ACTIVE_KEYS
            )
        )

    def _create_totp_cred(self):
        totp_cred = unit.new_totp_credential(self.user_id, self.project_id)
        self.credential_api.create_credential(uuid.uuid4().hex, totp_cred)

        def cleanup(testcase):
            totp_creds = testcase.credential_api.list_credentials_for_user(
                testcase.user['id'], type='totp')

            for cred in totp_creds:
                testcase.credential_api.delete_credential(cred['id'])

        self.addCleanup(cleanup, testcase=self)
        return totp_cred

    def auth_plugin_config_override(self, methods=None, **method_classes):
        methods = ['totp', 'token', 'password']
        super(TestMFARules, self).auth_plugin_config_override(methods)

    def _update_user_with_MFA_rules(self, rule_list, rules_enabled=True):
        user = self.user.copy()
        # Do not update password
        user.pop('password')
        user['options'][ro.MFA_RULES_OPT.option_name] = rule_list
        user['options'][ro.MFA_ENABLED_OPT.option_name] = rules_enabled
        self.identity_api.update_user(user['id'], user)

    def test_MFA_single_method_rules_requirements_met_succeeds(self):
        # ensure that a simple password works if a password-only rules exists
        rule_list = [['password'], ['password', 'totp']]
        self._update_user_with_MFA_rules(rule_list=rule_list)
        # NOTE(notmorgan): Step forward in time to ensure we're not causing
        # issues with revocation events that occur at the same time as the
        # token issuance. This is a bug with the limited resolution that
        # tokens and revocation events have.
        time = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
        with freezegun.freeze_time(time):
            self.v3_create_token(
                self.build_authentication_request(
                    user_id=self.user_id,
                    password=self.user['password'],
                    user_domain_id=self.domain_id,
                    project_id=self.project_id))

    def test_MFA_multi_method_rules_requirements_met_succeeds(self):
        # validate that multiple auth-methods function if all are specified
        # and the rules requires it
        rule_list = [['password', 'totp']]
        totp_cred = self._create_totp_cred()
        self._update_user_with_MFA_rules(rule_list=rule_list)
        # NOTE(notmorgan): Step forward in time to ensure we're not causing
        # issues with revocation events that occur at the same time as the
        # token issuance. This is a bug with the limited resolution that
        # tokens and revocation events have.
        time = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
        with freezegun.freeze_time(time):
            auth_req = self.build_authentication_request(
                user_id=self.user_id,
                password=self.user['password'],
                user_domain_id=self.domain_id,
                passcode=totp._generate_totp_passcode(totp_cred['blob']))
            self.v3_create_token(auth_req)

    def test_MFA_single_method_rules_requirements_not_met_fails(self):
        # if a rule matching a single auth type is specified and is not matched
        # the result should be unauthorized
        rule_list = [['totp']]
        self._update_user_with_MFA_rules(rule_list=rule_list)
        # NOTE(notmorgan): Step forward in time to ensure we're not causing
        # issues with revocation events that occur at the same time as the
        # token issuance. This is a bug with the limited resolution that
        # tokens and revocation events have.
        time = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
        with freezegun.freeze_time(time):
            self.v3_create_token(
                self.build_authentication_request(
                    user_id=self.user_id,
                    password=self.user['password'],
                    user_domain_id=self.domain_id,
                    project_id=self.project_id),
                expected_status=http_client.UNAUTHORIZED)

    def test_MFA_multi_method_rules_requirements_not_met_fails(self):
        # if multiple rules are specified and only one is passed,
        # unauthorized is expected
        rule_list = [['password', 'totp']]
        self._update_user_with_MFA_rules(rule_list=rule_list)
        # NOTE(notmorgan): Step forward in time to ensure we're not causing
        # issues with revocation events that occur at the same time as the
        # token issuance. This is a bug with the limited resolution that
        # tokens and revocation events have.
        time = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
        with freezegun.freeze_time(time):
            self.v3_create_token(
                self.build_authentication_request(
                    user_id=self.user_id,
                    password=self.user['password'],
                    user_domain_id=self.domain_id,
                    project_id=self.project_id),
                expected_status=http_client.UNAUTHORIZED)

    def test_MFA_rules_bogus_non_existing_auth_method_succeeds(self):
        # Bogus auth methods are thrown out from rules.
        rule_list = [['password'], ['BoGusAuThMeTh0dHandl3r']]
        self._update_user_with_MFA_rules(rule_list=rule_list)
        # NOTE(notmorgan): Step forward in time to ensure we're not causing
        # issues with revocation events that occur at the same time as the
        # token issuance. This is a bug with the limited resolution that
        # tokens and revocation events have.
        time = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
        with freezegun.freeze_time(time):
            self.v3_create_token(
                self.build_authentication_request(
                    user_id=self.user_id,
                    password=self.user['password'],
                    user_domain_id=self.domain_id,
                    project_id=self.project_id))

    def test_MFA_rules_disabled_MFA_succeeeds(self):
        # ensure that if MFA is "disableD" authentication succeeds, even if
        # not enough auth methods are specified
        rule_list = [['password', 'totp']]
        self._update_user_with_MFA_rules(rule_list=rule_list,
                                         rules_enabled=False)
        time = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
        # NOTE(notmorgan): Step forward in time to ensure we're not causing
        # issues with revocation events that occur at the same time as the
        # token issuance. This is a bug with the limited resolution that
        # tokens and revocation events have.
        with freezegun.freeze_time(time):
            self.v3_create_token(
                self.build_authentication_request(
                    user_id=self.user_id,
                    password=self.user['password'],
                    user_domain_id=self.domain_id,
                    project_id=self.project_id))

    def test_MFA_rules_all_bogus_rules_results_in_default_behavior(self):
        # if all the rules are bogus, the result is the same as the default
        # behavior, any single password method is sufficient
        rule_list = [[uuid.uuid4().hex, uuid.uuid4().hex],
                     ['BoGus'],
                     ['NonExistantMethod']]
        self._update_user_with_MFA_rules(rule_list=rule_list)
        # NOTE(notmorgan): Step forward in time to ensure we're not causing
        # issues with revocation events that occur at the same time as the
        # token issuance. This is a bug with the limited resolution that
        # tokens and revocation events have.
        time = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
        with freezegun.freeze_time(time):
            self.v3_create_token(
                self.build_authentication_request(
                    user_id=self.user_id,
                    password=self.user['password'],
                    user_domain_id=self.domain_id,
                    project_id=self.project_id))

    def test_MFA_rules_rescope_works_without_token_method_in_rules(self):
        rule_list = [['password', 'totp']]
        totp_cred = self._create_totp_cred()
        self._update_user_with_MFA_rules(rule_list=rule_list)
        # NOTE(notmorgan): Step forward in time to ensure we're not causing
        # issues with revocation events that occur at the same time as the
        # token issuance. This is a bug with the limited resolution that
        # tokens and revocation events have.
        time = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
        with freezegun.freeze_time(time):
            auth_data = self.build_authentication_request(
                user_id=self.user_id,
                password=self.user['password'],
                user_domain_id=self.domain_id,
                passcode=totp._generate_totp_passcode(totp_cred['blob']))
            r = self.v3_create_token(auth_data)
            auth_data = self.build_authentication_request(
                token=r.headers.get('X-Subject-Token'),
                project_id=self.project_id)
            self.v3_create_token(auth_data)


class TestAuthInfo(common_auth.AuthTestMixin, testcase.TestCase):
    def setUp(self):
        super(TestAuthInfo, self).setUp()
        auth.core.load_auth_methods()

    def test_unsupported_auth_method(self):
        auth_data = {'methods': ['abc']}
        auth_data['abc'] = {'test': 'test'}
        auth_data = {'identity': auth_data}
        self.assertRaises(exception.AuthMethodNotSupported,
                          auth.core.AuthInfo.create,
                          auth_data)

    def test_missing_auth_method_data(self):
        auth_data = {'methods': ['password']}
        auth_data = {'identity': auth_data}
        self.assertRaises(exception.ValidationError,
                          auth.core.AuthInfo.create,
                          auth_data)

    def test_project_name_no_domain(self):
        auth_data = self.build_authentication_request(
            username='test',
            password='test',
            project_name='abc')['auth']
        self.assertRaises(exception.ValidationError,
                          auth.core.AuthInfo.create,
                          auth_data)

    def test_both_project_and_domain_in_scope(self):
        auth_data = self.build_authentication_request(
            user_id='test',
            password='test',
            project_name='test',
            domain_name='test')['auth']
        self.assertRaises(exception.ValidationError,
                          auth.core.AuthInfo.create,
                          auth_data)

    def test_get_method_names_duplicates(self):
        auth_data = self.build_authentication_request(
            token='test',
            user_id='test',
            password='test')['auth']
        auth_data['identity']['methods'] = ['password', 'token',
                                            'password', 'password']
        auth_info = auth.core.AuthInfo.create(auth_data)
        self.assertEqual(['password', 'token'],
                         auth_info.get_method_names())

    def test_get_method_data_invalid_method(self):
        auth_data = self.build_authentication_request(
            user_id='test',
            password='test')['auth']
        auth_info = auth.core.AuthInfo.create(auth_data)

        method_name = uuid.uuid4().hex
        self.assertRaises(exception.ValidationError,
                          auth_info.get_method_data,
                          method_name)


class TokenAPITests(object):
    # Why is this not just setUp? Because TokenAPITests is not a test class
    # itself. If TokenAPITests became a subclass of the testcase, it would get
    # called by the enumerate-tests-in-file code. The way the functions get
    # resolved in Python for multiple inheritance means that a setUp in this
    # would get skipped by the testrunner.
    def doSetUp(self):
        r = self.v3_create_token(self.build_authentication_request(
            username=self.user['name'],
            user_domain_id=self.domain_id,
            password=self.user['password']))
        self.v3_token_data = r.result
        self.v3_token = r.headers.get('X-Subject-Token')
        self.headers = {'X-Subject-Token': r.headers.get('X-Subject-Token')}

    def _get_unscoped_token(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        r = self.post('/auth/tokens', body=auth_data)
        self.assertValidUnscopedTokenResponse(r)
        return r.headers.get('X-Subject-Token')

    def _get_domain_scoped_token(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            domain_id=self.domain_id)
        r = self.post('/auth/tokens', body=auth_data)
        self.assertValidDomainScopedTokenResponse(r)
        return r.headers.get('X-Subject-Token')

    def _get_project_scoped_token(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project_id)
        r = self.post('/auth/tokens', body=auth_data)
        self.assertValidProjectScopedTokenResponse(r)
        return r.headers.get('X-Subject-Token')

    def _get_trust_scoped_token(self, trustee_user, trust):
        auth_data = self.build_authentication_request(
            user_id=trustee_user['id'],
            password=trustee_user['password'],
            trust_id=trust['id'])
        r = self.post('/auth/tokens', body=auth_data)
        self.assertValidProjectScopedTokenResponse(r)
        return r.headers.get('X-Subject-Token')

    def _create_trust(self, impersonation=False):
        # Create a trustee user
        trustee_user = unit.create_user(self.identity_api,
                                        domain_id=self.domain_id)
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=trustee_user['id'],
            project_id=self.project_id,
            impersonation=impersonation,
            role_ids=[self.role_id])

        # Create a trust
        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        trust = self.assertValidTrustResponse(r)
        return (trustee_user, trust)

    def _validate_token(self, token,
                        expected_status=http_client.OK, allow_expired=False):
        path = '/v3/auth/tokens'

        if allow_expired:
            path += '?allow_expired=1'

        return self.admin_request(
            path=path,
            headers={'X-Auth-Token': self.get_admin_token(),
                     'X-Subject-Token': token},
            method='GET',
            expected_status=expected_status
        )

    def _validate_token_v2(self, token, expected_status=http_client.OK):
        return self.admin_request(
            path='/v2.0/tokens/%s' % token,
            headers={'X-Auth-Token': self.get_scoped_token()},
            method='GET',
            expected_status=expected_status
        )

    def _revoke_token(self, token, expected_status=http_client.NO_CONTENT):
        return self.delete(
            '/auth/tokens',
            headers={'x-subject-token': token},
            expected_status=expected_status)

    def _set_user_enabled(self, user, enabled=True):
        user['enabled'] = enabled
        self.identity_api.update_user(user['id'], user)

    def _create_project_and_set_as_default_project(self):
        # create a new project
        ref = unit.new_project_ref(domain_id=self.domain_id)
        r = self.post('/projects', body={'project': ref})
        project = self.assertValidProjectResponse(r, ref)

        # grant the user a role on the project
        self.put(
            '/projects/%(project_id)s/users/%(user_id)s/roles/%(role_id)s' % {
                'user_id': self.user['id'],
                'project_id': project['id'],
                'role_id': self.role['id']})

        # make the new project the user's default project
        body = {'user': {'default_project_id': project['id']}}
        r = self.patch('/users/%(user_id)s' % {
            'user_id': self.user['id']},
            body=body)
        self.assertValidUserResponse(r)

        return project

    def test_auth_with_token_as_different_user_fails(self):
        # get the token for a user. This is self.user which is different from
        # self.default_domain_user.
        token = self.get_scoped_token()
        # try both password and token methods with different identities and it
        # should fail
        auth_data = self.build_authentication_request(
            token=token,
            user_id=self.default_domain_user['id'],
            password=self.default_domain_user['password'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_token_for_user_without_password_fails(self):
        user = unit.new_user_ref(domain_id=self.domain['id'])
        del user['password']  # can't have a password for this test
        user = self.identity_api.create_user(user)

        auth_data = self.build_authentication_request(
            user_id=user['id'],
            password='password')

        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_unscoped_token_by_authenticating_with_unscoped_token(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        r = self.v3_create_token(auth_data)
        self.assertValidUnscopedTokenResponse(r)
        token_id = r.headers.get('X-Subject-Token')

        auth_data = self.build_authentication_request(token=token_id)
        r = self.v3_create_token(auth_data)
        self.assertValidUnscopedTokenResponse(r)

    def test_create_unscoped_token_with_user_id(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        r = self.v3_create_token(auth_data)
        self.assertValidUnscopedTokenResponse(r)

    def test_create_unscoped_token_with_user_domain_id(self):
        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_id=self.domain['id'],
            password=self.user['password'])
        r = self.v3_create_token(auth_data)
        self.assertValidUnscopedTokenResponse(r)

    def test_create_unscoped_token_with_user_domain_name(self):
        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_name=self.domain['name'],
            password=self.user['password'])
        r = self.v3_create_token(auth_data)
        self.assertValidUnscopedTokenResponse(r)

    def test_validate_unscoped_token(self):
        unscoped_token = self._get_unscoped_token()
        r = self._validate_token(unscoped_token)
        self.assertValidUnscopedTokenResponse(r)

    def test_validate_expired_unscoped_token_returns_not_found(self):
        # NOTE(lbragstad): We set token expiration to 10 seconds so that we can
        # use the context manager of freezegun without sqlite issues.
        self.config_fixture.config(group='token',
                                   expiration=10)
        time = datetime.datetime.utcnow()
        with freezegun.freeze_time(time) as frozen_datetime:
            unscoped_token = self._get_unscoped_token()
            frozen_datetime.tick(delta=datetime.timedelta(seconds=15))
            self._validate_token(
                unscoped_token,
                expected_status=http_client.NOT_FOUND
            )

    def test_revoke_unscoped_token(self):
        unscoped_token = self._get_unscoped_token()
        r = self._validate_token(unscoped_token)
        self.assertValidUnscopedTokenResponse(r)
        self._revoke_token(unscoped_token)
        self._validate_token(unscoped_token,
                             expected_status=http_client.NOT_FOUND)

    def test_create_explicit_unscoped_token(self):
        self._create_project_and_set_as_default_project()

        # explicitly ask for an unscoped token
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            unscoped="unscoped")
        r = self.post('/auth/tokens', body=auth_data, noauth=True)
        self.assertValidUnscopedTokenResponse(r)

    def test_disabled_users_default_project_result_in_unscoped_token(self):
        # create a disabled project to work with
        project = self.create_new_default_project_for_user(
            self.user['id'], self.domain_id, enable_project=False)

        # assign a role to user for the new project
        self.assignment_api.add_role_to_user_and_project(self.user['id'],
                                                         project['id'],
                                                         self.role_id)

        # attempt to authenticate without requesting a project
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        r = self.v3_create_token(auth_data)
        self.assertValidUnscopedTokenResponse(r)

    def test_disabled_default_project_domain_result_in_unscoped_token(self):
        domain_ref = unit.new_domain_ref()
        r = self.post('/domains', body={'domain': domain_ref})
        domain = self.assertValidDomainResponse(r, domain_ref)

        project = self.create_new_default_project_for_user(
            self.user['id'], domain['id'])

        # assign a role to user for the new project
        self.assignment_api.add_role_to_user_and_project(self.user['id'],
                                                         project['id'],
                                                         self.role_id)

        # now disable the project domain
        body = {'domain': {'enabled': False}}
        r = self.patch('/domains/%(domain_id)s' % {'domain_id': domain['id']},
                       body=body)
        self.assertValidDomainResponse(r)

        # attempt to authenticate without requesting a project
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        r = self.v3_create_token(auth_data)
        self.assertValidUnscopedTokenResponse(r)

    def test_unscoped_token_is_invalid_after_disabling_user(self):
        unscoped_token = self._get_unscoped_token()
        # Make sure the token is valid
        r = self._validate_token(unscoped_token)
        self.assertValidUnscopedTokenResponse(r)
        # Disable the user
        self._set_user_enabled(self.user, enabled=False)
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            unscoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_unscoped_token_is_invalid_after_enabling_disabled_user(self):
        unscoped_token = self._get_unscoped_token()
        # Make sure the token is valid
        r = self._validate_token(unscoped_token)
        self.assertValidUnscopedTokenResponse(r)
        # Disable the user
        self._set_user_enabled(self.user, enabled=False)
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            unscoped_token,
            expected_status=http_client.NOT_FOUND
        )
        # Enable the user
        self._set_user_enabled(self.user)
        # Ensure validating a token for a re-enabled user fails
        self._validate_token(
            unscoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_unscoped_token_is_invalid_after_disabling_user_domain(self):
        unscoped_token = self._get_unscoped_token()
        # Make sure the token is valid
        r = self._validate_token(unscoped_token)
        self.assertValidUnscopedTokenResponse(r)
        # Disable the user's domain
        self.domain['enabled'] = False
        self.resource_api.update_domain(self.domain['id'], self.domain)
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            unscoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_unscoped_token_is_invalid_after_changing_user_password(self):
        unscoped_token = self._get_unscoped_token()
        # Make sure the token is valid
        r = self._validate_token(unscoped_token)
        self.assertValidUnscopedTokenResponse(r)
        # Change user's password
        self.user['password'] = 'Password1'
        self.identity_api.update_user(self.user['id'], self.user)
        # Ensure updating user's password revokes existing user's tokens
        self._validate_token(
            unscoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_create_domain_token_scoped_with_domain_id_and_user_id(self):
        # grant the user a role on the domain
        path = '/domains/%s/users/%s/roles/%s' % (
            self.domain['id'], self.user['id'], self.role['id'])
        self.put(path=path)

        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            domain_id=self.domain['id'])
        r = self.v3_create_token(auth_data)
        self.assertValidDomainScopedTokenResponse(r)

    def test_create_domain_token_scoped_with_domain_id_and_username(self):
        # grant the user a role on the domain
        path = '/domains/%s/users/%s/roles/%s' % (
            self.domain['id'], self.user['id'], self.role['id'])
        self.put(path=path)

        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_id=self.domain['id'],
            password=self.user['password'],
            domain_id=self.domain['id'])
        r = self.v3_create_token(auth_data)
        self.assertValidDomainScopedTokenResponse(r)

    def test_create_domain_token_scoped_with_domain_id(self):
        # grant the user a role on the domain
        path = '/domains/%s/users/%s/roles/%s' % (
            self.domain['id'], self.user['id'], self.role['id'])
        self.put(path=path)

        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_name=self.domain['name'],
            password=self.user['password'],
            domain_id=self.domain['id'])
        r = self.v3_create_token(auth_data)
        self.assertValidDomainScopedTokenResponse(r)

    def test_create_domain_token_scoped_with_domain_name(self):
        # grant the user a role on the domain
        path = '/domains/%s/users/%s/roles/%s' % (
            self.domain['id'], self.user['id'], self.role['id'])
        self.put(path=path)

        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            domain_name=self.domain['name'])
        r = self.v3_create_token(auth_data)
        self.assertValidDomainScopedTokenResponse(r)

    def test_create_domain_token_scoped_with_domain_name_and_username(self):
        # grant the user a role on the domain
        path = '/domains/%s/users/%s/roles/%s' % (
            self.domain['id'], self.user['id'], self.role['id'])
        self.put(path=path)

        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_id=self.domain['id'],
            password=self.user['password'],
            domain_name=self.domain['name'])
        r = self.v3_create_token(auth_data)
        self.assertValidDomainScopedTokenResponse(r)

    def test_create_domain_token_with_only_domain_name_and_username(self):
        # grant the user a role on the domain
        path = '/domains/%s/users/%s/roles/%s' % (
            self.domain['id'], self.user['id'], self.role['id'])
        self.put(path=path)

        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_name=self.domain['name'],
            password=self.user['password'],
            domain_name=self.domain['name'])
        r = self.v3_create_token(auth_data)
        self.assertValidDomainScopedTokenResponse(r)

    def test_create_domain_token_with_group_role(self):
        group = unit.new_group_ref(domain_id=self.domain_id)
        group = self.identity_api.create_group(group)

        # add user to group
        self.identity_api.add_user_to_group(self.user['id'], group['id'])

        # grant the domain role to group
        path = '/domains/%s/groups/%s/roles/%s' % (
            self.domain['id'], group['id'], self.role['id'])
        self.put(path=path)

        # now get a domain-scoped token
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            domain_id=self.domain['id'])
        r = self.v3_create_token(auth_data)
        self.assertValidDomainScopedTokenResponse(r)

    def test_create_domain_token_fails_if_domain_name_unsafe(self):
        """Verify authenticate to a domain with unsafe name fails."""
        # Start with url name restrictions off, so we can create the unsafe
        # named domain
        self.config_fixture.config(group='resource',
                                   domain_name_url_safe='off')
        unsafe_name = 'i am not / safe'
        domain = unit.new_domain_ref(name=unsafe_name)
        self.resource_api.create_domain(domain['id'], domain)
        role_member = unit.new_role_ref()
        self.role_api.create_role(role_member['id'], role_member)
        self.assignment_api.create_grant(
            role_member['id'],
            user_id=self.user['id'],
            domain_id=domain['id'])

        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            domain_name=domain['name'])

        # Since name url restriction is off, we should be able to authenticate
        self.v3_create_token(auth_data)

        # Set the name url restriction to new, which should still allow us to
        # authenticate
        self.config_fixture.config(group='resource',
                                   project_name_url_safe='new')
        self.v3_create_token(auth_data)

        # Set the name url restriction to strict and we should fail to
        # authenticate
        self.config_fixture.config(group='resource',
                                   domain_name_url_safe='strict')
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_domain_token_without_grant_returns_unauthorized(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            domain_id=self.domain['id'])
        # this fails because the user does not have a role on self.domain
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_validate_domain_scoped_token(self):
        # Grant user access to domain
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domain['id'])
        domain_scoped_token = self._get_domain_scoped_token()
        r = self._validate_token(domain_scoped_token)
        self.assertValidDomainScopedTokenResponse(r)
        resp_json = json.loads(r.body)
        self.assertIsNotNone(resp_json['token']['catalog'])
        self.assertIsNotNone(resp_json['token']['roles'])
        self.assertIsNotNone(resp_json['token']['domain'])

    def test_validate_expired_domain_scoped_token_returns_not_found(self):
        # Grant user access to domain
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domain['id'])
        # NOTE(lbragstad): We set token expiration to 10 seconds so that we can
        # use the context manager of freezegun without sqlite issues.
        self.config_fixture.config(group='token',
                                   expiration=10)
        time = datetime.datetime.utcnow()
        with freezegun.freeze_time(time) as frozen_datetime:
            domain_scoped_token = self._get_domain_scoped_token()
            frozen_datetime.tick(delta=datetime.timedelta(seconds=15))
            self._validate_token(
                domain_scoped_token,
                expected_status=http_client.NOT_FOUND
            )

    def test_domain_scoped_token_is_invalid_after_disabling_user(self):
        # Grant user access to domain
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domain['id'])
        domain_scoped_token = self._get_domain_scoped_token()
        # Make sure the token is valid
        r = self._validate_token(domain_scoped_token)
        self.assertValidDomainScopedTokenResponse(r)
        # Disable user
        self._set_user_enabled(self.user, enabled=False)
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            domain_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_domain_scoped_token_is_invalid_after_deleting_grant(self):
        # Grant user access to domain
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domain['id'])
        domain_scoped_token = self._get_domain_scoped_token()
        # Make sure the token is valid
        r = self._validate_token(domain_scoped_token)
        self.assertValidDomainScopedTokenResponse(r)
        # Delete access to domain
        self.assignment_api.delete_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domain['id'])
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            domain_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_domain_scoped_token_invalid_after_disabling_domain(self):
        # Grant user access to domain
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domain['id'])
        domain_scoped_token = self._get_domain_scoped_token()
        # Make sure the token is valid
        r = self._validate_token(domain_scoped_token)
        self.assertValidDomainScopedTokenResponse(r)
        # Disable domain
        self.domain['enabled'] = False
        self.resource_api.update_domain(self.domain['id'], self.domain)
        # Ensure validating a token for a disabled domain fails
        self._validate_token(
            domain_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_v2_validate_domain_scoped_token_returns_unauthorized(self):
        # Test that validating a domain scoped token in v2.0 returns
        # unauthorized.
        # Grant user access to domain
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domain['id'])

        scoped_token = self._get_domain_scoped_token()
        self._validate_token_v2(
            scoped_token,
            expected_status=http_client.UNAUTHORIZED
        )

    def test_create_project_scoped_token_with_project_id_and_user_id(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id'])
        r = self.v3_create_token(auth_data)
        self.assertValidProjectScopedTokenResponse(r)

    def test_validate_project_scoped_token(self):
        project_scoped_token = self._get_project_scoped_token()
        r = self._validate_token(project_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)

    def test_validate_expired_project_scoped_token_returns_not_found(self):
        # NOTE(lbragstad): We set token expiration to 10 seconds so that we can
        # use the context manager of freezegun without sqlite issues.
        self.config_fixture.config(group='token',
                                   expiration=10)
        time = datetime.datetime.utcnow()
        with freezegun.freeze_time(time) as frozen_datetime:
            project_scoped_token = self._get_project_scoped_token()
            frozen_datetime.tick(delta=datetime.timedelta(seconds=15))
            self._validate_token(
                project_scoped_token,
                expected_status=http_client.NOT_FOUND
            )

    def test_revoke_project_scoped_token(self):
        project_scoped_token = self._get_project_scoped_token()
        r = self._validate_token(project_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)
        self._revoke_token(project_scoped_token)
        self._validate_token(project_scoped_token,
                             expected_status=http_client.NOT_FOUND)

    def test_project_scoped_token_is_scoped_to_default_project(self):
        project = self._create_project_and_set_as_default_project()

        # attempt to authenticate without requesting a project
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        r = self.v3_create_token(auth_data)

        # ensure the project id in the token matches the default project id
        self.assertValidProjectScopedTokenResponse(r)
        self.assertEqual(project['id'], r.result['token']['project']['id'])

    def test_project_scoped_token_no_catalog_is_scoped_to_default_project(
            self):
        project = self._create_project_and_set_as_default_project()

        # attempt to authenticate without requesting a project or catalog
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        r = self.post('/auth/tokens?nocatalog', body=auth_data, noauth=True)

        # ensure the project id in the token matches the default project id
        self.assertValidProjectScopedTokenResponse(r, require_catalog=False)
        self.assertEqual(project['id'], r.result['token']['project']['id'])

    def test_implicit_project_id_scoped_token_with_user_id_no_catalog(self):
        self._create_project_and_set_as_default_project()

        # create a project scoped token that isn't scoped to the default
        # project
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id'])
        r = self.post('/auth/tokens?nocatalog', body=auth_data, noauth=True)

        # ensure the project id in the token matches the one we as for
        self.assertValidProjectScopedTokenResponse(r, require_catalog=False)
        self.assertEqual(self.project['id'],
                         r.result['token']['project']['id'])

    def test_project_scoped_token_catalog_attributes(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id'])
        r = self.v3_create_token(auth_data)

        catalog = r.result['token']['catalog']
        self.assertEqual(1, len(catalog))
        catalog = catalog[0]

        self.assertEqual(self.service['id'], catalog['id'])
        self.assertEqual(self.service['name'], catalog['name'])
        self.assertEqual(self.service['type'], catalog['type'])

        endpoint = catalog['endpoints']
        self.assertEqual(1, len(endpoint))
        endpoint = endpoint[0]

        self.assertEqual(self.endpoint['id'], endpoint['id'])
        self.assertEqual(self.endpoint['interface'], endpoint['interface'])
        self.assertEqual(self.endpoint['region_id'], endpoint['region_id'])
        self.assertEqual(self.endpoint['url'], endpoint['url'])

    def test_project_scoped_token_catalog_excludes_disabled_endpoint(self):
        # Create a disabled endpoint
        disabled_endpoint_ref = copy.copy(self.endpoint)
        disabled_endpoint_id = uuid.uuid4().hex
        disabled_endpoint_ref.update({
            'id': disabled_endpoint_id,
            'enabled': False,
            'interface': 'internal'
        })
        self.catalog_api.create_endpoint(disabled_endpoint_id,
                                         disabled_endpoint_ref)

        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id'])
        resp = self.v3_create_token(auth_data)

        # make sure the disabled endpoint id isn't in the list of endpoints
        endpoints = resp.result['token']['catalog'][0]['endpoints']
        endpoint_ids = [endpoint['id'] for endpoint in endpoints]
        self.assertNotIn(disabled_endpoint_id, endpoint_ids)

    def test_project_scoped_token_catalog_excludes_disabled_service(self):
        """On authenticate, get a catalog that excludes disabled services."""
        # although the endpoint associated with the service is enabled, the
        # service is disabled
        self.assertTrue(self.endpoint['enabled'])
        self.catalog_api.update_service(
            self.endpoint['service_id'], {'enabled': False})
        service = self.catalog_api.get_service(self.endpoint['service_id'])
        self.assertFalse(service['enabled'])

        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id'])
        r = self.v3_create_token(auth_data)

        self.assertEqual([], r.result['token']['catalog'])

    def test_scope_to_project_without_grant_returns_unauthorized(self):
        project = unit.new_project_ref(domain_id=self.domain_id)
        self.resource_api.create_project(project['id'], project)

        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=project['id'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_project_scoped_token_with_username_and_domain_id(self):
        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_id=self.domain['id'],
            password=self.user['password'],
            project_id=self.project['id'])
        r = self.v3_create_token(auth_data)
        self.assertValidProjectScopedTokenResponse(r)

    def test_create_project_scoped_token_with_username_and_domain_name(self):
        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_name=self.domain['name'],
            password=self.user['password'],
            project_id=self.project['id'])
        r = self.v3_create_token(auth_data)
        self.assertValidProjectScopedTokenResponse(r)

    def test_create_project_scoped_token_fails_if_project_name_unsafe(self):
        """Verify authenticate to a project with unsafe name fails."""
        # Start with url name restrictions off, so we can create the unsafe
        # named project
        self.config_fixture.config(group='resource',
                                   project_name_url_safe='off')
        unsafe_name = 'i am not / safe'
        project = unit.new_project_ref(domain_id=test_v3.DEFAULT_DOMAIN_ID,
                                       name=unsafe_name)
        self.resource_api.create_project(project['id'], project)
        role_member = unit.new_role_ref()
        self.role_api.create_role(role_member['id'], role_member)
        self.assignment_api.add_role_to_user_and_project(
            self.user['id'], project['id'], role_member['id'])

        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_name=project['name'],
            project_domain_id=test_v3.DEFAULT_DOMAIN_ID)

        # Since name url restriction is off, we should be able to authenticate
        self.v3_create_token(auth_data)

        # Set the name url restriction to new, which should still allow us to
        # authenticate
        self.config_fixture.config(group='resource',
                                   project_name_url_safe='new')
        self.v3_create_token(auth_data)

        # Set the name url restriction to strict and we should fail to
        # authenticate
        self.config_fixture.config(group='resource',
                                   project_name_url_safe='strict')
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_project_scoped_token_fails_if_domain_name_unsafe(self):
        """Verify authenticate to a project using unsafe domain name fails."""
        # Start with url name restrictions off, so we can create the unsafe
        # named domain
        self.config_fixture.config(group='resource',
                                   domain_name_url_safe='off')
        unsafe_name = 'i am not / safe'
        domain = unit.new_domain_ref(name=unsafe_name)
        self.resource_api.create_domain(domain['id'], domain)
        # Add a (safely named) project to that domain
        project = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project['id'], project)
        role_member = unit.new_role_ref()
        self.role_api.create_role(role_member['id'], role_member)
        self.assignment_api.create_grant(
            role_member['id'],
            user_id=self.user['id'],
            project_id=project['id'])

        # An auth request via project ID, but specifying domain by name
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_name=project['name'],
            project_domain_name=domain['name'])

        # Since name url restriction is off, we should be able to authenticate
        self.v3_create_token(auth_data)

        # Set the name url restriction to new, which should still allow us to
        # authenticate
        self.config_fixture.config(group='resource',
                                   project_name_url_safe='new')
        self.v3_create_token(auth_data)

        # Set the name url restriction to strict and we should fail to
        # authenticate
        self.config_fixture.config(group='resource',
                                   domain_name_url_safe='strict')
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_project_token_with_same_domain_and_project_name(self):
        """Authenticate to a project with the same name as its domain."""
        domain = unit.new_project_ref(is_domain=True)
        domain = self.resource_api.create_project(domain['id'], domain)
        project = unit.new_project_ref(domain_id=domain['id'],
                                       name=domain['name'])
        self.resource_api.create_project(project['id'], project)
        role_member = unit.new_role_ref()
        self.role_api.create_role(role_member['id'], role_member)
        self.assignment_api.add_role_to_user_and_project(
            self.user['id'], project['id'], role_member['id'])

        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_name=project['name'],
            project_domain_name=domain['name'])

        r = self.v3_create_token(auth_data)
        self.assertEqual(project['id'], r.result['token']['project']['id'])

    def test_create_project_token_fails_with_project_acting_as_domain(self):
        domain = unit.new_project_ref(is_domain=True)
        domain = self.resource_api.create_project(domain['id'], domain)
        role_member = unit.new_role_ref()
        self.role_api.create_role(role_member['id'], role_member)
        self.assignment_api.create_grant(
            role_member['id'],
            user_id=self.user['id'],
            domain_id=domain['id'])

        # authentication will fail because the project name is incorrect
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_name=domain['name'],
            project_domain_name=domain['name'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_project_token_with_disabled_project_domain_fails(self):
        # create a disabled domain
        domain = unit.new_domain_ref()
        domain = self.resource_api.create_domain(domain['id'], domain)

        # create a project in the domain
        project = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project['id'], project)

        # assign some role to self.user for the project in the domain
        self.assignment_api.add_role_to_user_and_project(
            self.user['id'],
            project['id'],
            self.role_id)

        # Disable the domain
        domain['enabled'] = False
        self.resource_api.update_domain(domain['id'], domain)

        # user should not be able to auth with project_id
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=project['id'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # user should not be able to auth with project_name & domain
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_name=project['name'],
            project_domain_id=domain['id'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_project_scoped_token_is_invalid_after_disabling_user(self):
        project_scoped_token = self._get_project_scoped_token()
        # Make sure the token is valid
        r = self._validate_token(project_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)
        # Disable the user
        self._set_user_enabled(self.user, enabled=False)
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            project_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_project_scoped_token_invalid_after_changing_user_password(self):
        project_scoped_token = self._get_project_scoped_token()
        # Make sure the token is valid
        r = self._validate_token(project_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)
        # Update user's password
        self.user['password'] = 'Password1'
        self.identity_api.update_user(self.user['id'], self.user)
        # Ensure updating user's password revokes existing tokens
        self._validate_token(
            project_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_project_scoped_token_invalid_after_disabling_project(self):
        project_scoped_token = self._get_project_scoped_token()
        # Make sure the token is valid
        r = self._validate_token(project_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)
        # Disable project
        self.project['enabled'] = False
        self.resource_api.update_project(self.project['id'], self.project)
        # Ensure validating a token for a disabled project fails
        self._validate_token(
            project_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_project_scoped_token_is_invalid_after_deleting_grant(self):
        # disable caching so that user grant deletion is not hidden
        # by token caching
        self.config_fixture.config(
            group='cache',
            enabled=False)
        # Grant user access to project
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         project_id=self.project['id'])
        project_scoped_token = self._get_project_scoped_token()
        # Make sure the token is valid
        r = self._validate_token(project_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)
        # Delete access to project
        self.assignment_api.delete_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         project_id=self.project['id'])
        # Ensure the token has been revoked
        self._validate_token(
            project_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_no_access_to_default_project_result_in_unscoped_token(self):
        # create a disabled project to work with
        self.create_new_default_project_for_user(self.user['id'],
                                                 self.domain_id)

        # attempt to authenticate without requesting a project
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        r = self.v3_create_token(auth_data)
        self.assertValidUnscopedTokenResponse(r)

    def test_rescope_unscoped_token_with_trust(self):
        trustee_user, trust = self._create_trust()
        self._get_trust_scoped_token(trustee_user, trust)

    def test_validate_a_trust_scoped_token(self):
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)

    def test_validate_expired_trust_scoped_token_returns_not_found(self):
        # NOTE(lbragstad): We set token expiration to 10 seconds so that we can
        # use the context manager of freezegun without sqlite issues.
        self.config_fixture.config(group='token',
                                   expiration=10)
        time = datetime.datetime.utcnow()
        with freezegun.freeze_time(time) as frozen_datetime:
            trustee_user, trust = self._create_trust()
            trust_scoped_token = self._get_trust_scoped_token(
                trustee_user, trust
            )
            frozen_datetime.tick(delta=datetime.timedelta(seconds=15))
            self._validate_token(
                trust_scoped_token,
                expected_status=http_client.NOT_FOUND
            )

    def test_validate_a_trust_scoped_token_impersonated(self):
        trustee_user, trust = self._create_trust(impersonation=True)
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)

    def test_revoke_trust_scoped_token(self):
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)
        self._revoke_token(trust_scoped_token)
        self._validate_token(trust_scoped_token,
                             expected_status=http_client.NOT_FOUND)

    def test_trust_scoped_token_is_invalid_after_disabling_trustee(self):
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)

        # Disable trustee
        trustee_update_ref = dict(enabled=False)
        self.identity_api.update_user(trustee_user['id'], trustee_update_ref)
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            trust_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_trust_token_is_invalid_when_trustee_domain_disabled(self):
        # create a new domain with new user in that domain
        new_domain_ref = unit.new_domain_ref()
        self.resource_api.create_domain(new_domain_ref['id'], new_domain_ref)

        trustee_ref = unit.create_user(self.identity_api,
                                       domain_id=new_domain_ref['id'])

        new_project_ref = unit.new_project_ref(domain_id=self.domain_id)
        self.resource_api.create_project(new_project_ref['id'],
                                         new_project_ref)

        # grant the trustor access to the new project
        self.assignment_api.create_grant(
            self.role['id'],
            user_id=self.user_id,
            project_id=new_project_ref['id'])

        trust_ref = unit.new_trust_ref(trustor_user_id=self.user_id,
                                       trustee_user_id=trustee_ref['id'],
                                       expires=dict(minutes=1),
                                       project_id=new_project_ref['id'],
                                       impersonation=True,
                                       role_ids=[self.role['id']])

        resp = self.post('/OS-TRUST/trusts', body={'trust': trust_ref})
        self.assertValidTrustResponse(resp, trust_ref)
        trust_id = resp.json_body['trust']['id']

        # get a project-scoped token using the trust
        trust_auth_data = self.build_authentication_request(
            user_id=trustee_ref['id'],
            password=trustee_ref['password'],
            trust_id=trust_id)
        trust_scoped_token = self.get_requested_token(trust_auth_data)

        # ensure the project-scoped token from the trust is valid
        self._validate_token(trust_scoped_token)

        disable_body = {'domain': {'enabled': False}}
        self.patch(
            '/domains/%(domain_id)s' % {'domain_id': new_domain_ref['id']},
            body=disable_body)

        # ensure the project-scoped token from the trust is invalid
        self._validate_token(trust_scoped_token,
                             expected_status=http_client.NOT_FOUND)

    def test_trust_scoped_token_invalid_after_changing_trustee_password(self):
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)
        # Change trustee's password
        trustee_update_ref = dict(password='Password1')
        self.identity_api.update_user(trustee_user['id'], trustee_update_ref)
        # Ensure updating trustee's password revokes existing tokens
        self._validate_token(
            trust_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_trust_scoped_token_is_invalid_after_disabling_trustor(self):
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)

        # Disable the trustor
        trustor_update_ref = dict(enabled=False)
        self.identity_api.update_user(self.user['id'], trustor_update_ref)
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            trust_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_trust_scoped_token_invalid_after_changing_trustor_password(self):
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)

        # Change trustor's password
        trustor_update_ref = dict(password='Password1')
        self.identity_api.update_user(self.user['id'], trustor_update_ref)
        # Ensure updating trustor's password revokes existing user's tokens
        self._validate_token(
            trust_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_trust_scoped_token_invalid_after_disabled_trustor_domain(self):
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)

        # Disable trustor's domain
        self.domain['enabled'] = False
        self.resource_api.update_domain(self.domain['id'], self.domain)

        trustor_update_ref = dict(password='Password1')
        self.identity_api.update_user(self.user['id'], trustor_update_ref)
        # Ensure updating trustor's password revokes existing user's tokens
        self._validate_token(
            trust_scoped_token,
            expected_status=http_client.NOT_FOUND
        )

    def test_validate_trust_token_on_v2_fails_outside_default_domain(self):
        # NOTE(lbragstad): This fails validation against the v2.0 API because
        # the actors of the trust are not within the default domain.
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        self._validate_token_v2(
            trust_scoped_token,
            expected_status=http_client.UNAUTHORIZED
        )

    def test_default_fixture_scope_token(self):
        self.assertIsNotNone(self.get_scoped_token())

    def test_v3_v2_intermix_new_default_domain(self):
        # If the default_domain_id config option is changed, then should be
        # able to validate a v3 token with user in the new domain.

        # 1) Create a new domain for the user.
        new_domain = unit.new_domain_ref()
        self.resource_api.create_domain(new_domain['id'], new_domain)

        # 2) Create user in new domain.
        new_user = unit.create_user(self.identity_api,
                                    domain_id=new_domain['id'])

        # 3) Update the default_domain_id config option to the new domain
        self.config_fixture.config(
            group='identity',
            default_domain_id=new_domain['id'])

        # 4) Get a token using v3 API.
        v3_token = self.get_requested_token(self.build_authentication_request(
            user_id=new_user['id'],
            password=new_user['password']))

        # 5) Validate token using v2 API.
        self.admin_request(
            path='/v2.0/tokens/%s' % v3_token,
            token=self.get_admin_token(),
            method='GET')

    def test_v3_v2_intermix_domain_scoped_token_failed(self):
        # grant the domain role to user
        self.put(
            path='/domains/%s/users/%s/roles/%s' % (
                self.domain['id'], self.user['id'], self.role['id']))

        # generate a domain-scoped v3 token
        v3_token = self.get_requested_token(self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            domain_id=self.domain['id']))

        # domain-scoped tokens are not supported by v2
        self.admin_request(
            method='GET',
            path='/v2.0/tokens/%s' % v3_token,
            token=self.get_admin_token(),
            expected_status=http_client.UNAUTHORIZED)

    def test_v3_v2_intermix_non_default_project_succeed(self):
        # self.project is in a non-default domain
        v3_token = self.get_requested_token(self.build_authentication_request(
            user_id=self.default_domain_user['id'],
            password=self.default_domain_user['password'],
            project_id=self.project['id']))

        # v2 cannot reference projects outside the default domain
        self.admin_request(
            method='GET',
            path='/v2.0/tokens/%s' % v3_token,
            token=self.get_admin_token())

    def test_v3_v2_intermix_non_default_user_succeed(self):
        self.assignment_api.create_grant(
            self.role['id'],
            user_id=self.user['id'],
            project_id=self.default_domain_project['id'])

        # self.user is in a non-default domain
        v3_token = self.get_requested_token(self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.default_domain_project['id']))

        # v2 cannot reference projects outside the default domain
        self.admin_request(
            method='GET',
            path='/v2.0/tokens/%s' % v3_token,
            token=self.get_admin_token())

    def test_v3_v2_intermix_domain_scope_failed(self):
        self.assignment_api.create_grant(
            self.role['id'],
            user_id=self.default_domain_user['id'],
            domain_id=self.domain['id'])

        v3_token = self.get_requested_token(self.build_authentication_request(
            user_id=self.default_domain_user['id'],
            password=self.default_domain_user['password'],
            domain_id=self.domain['id']))

        # v2 cannot reference projects outside the default domain
        self.admin_request(
            path='/v2.0/tokens/%s' % v3_token,
            token=self.get_admin_token(),
            method='GET',
            expected_status=http_client.UNAUTHORIZED)

    def test_v3_v2_unscoped_token_intermix(self):
        r = self.v3_create_token(self.build_authentication_request(
            user_id=self.default_domain_user['id'],
            password=self.default_domain_user['password']))
        self.assertValidUnscopedTokenResponse(r)
        v3_token_data = r.result
        v3_token = r.headers.get('X-Subject-Token')

        # now validate the v3 token with v2 API
        r = self.admin_request(
            path='/v2.0/tokens/%s' % v3_token,
            token=self.get_admin_token(),
            method='GET')
        v2_token_data = r.result

        self.assertEqual(v2_token_data['access']['user']['id'],
                         v3_token_data['token']['user']['id'])
        self.assertTimestampEqual(v2_token_data['access']['token']['expires'],
                                  v3_token_data['token']['expires_at'])

    def test_v3_v2_token_intermix(self):
        r = self.v3_create_token(self.build_authentication_request(
            user_id=self.default_domain_user['id'],
            password=self.default_domain_user['password'],
            project_id=self.default_domain_project['id']))
        self.assertValidProjectScopedTokenResponse(r)
        v3_token_data = r.result
        v3_token = r.headers.get('X-Subject-Token')

        # now validate the v3 token with v2 API
        r = self.admin_request(
            method='GET',
            path='/v2.0/tokens/%s' % v3_token,
            token=self.get_admin_token())
        v2_token_data = r.result

        self.assertEqual(v2_token_data['access']['user']['id'],
                         v3_token_data['token']['user']['id'])
        self.assertTimestampEqual(v2_token_data['access']['token']['expires'],
                                  v3_token_data['token']['expires_at'])
        self.assertEqual(v2_token_data['access']['user']['roles'][0]['name'],
                         v3_token_data['token']['roles'][0]['name'])

    def test_v2_v3_unscoped_token_intermix(self):
        r = self.admin_request(
            method='POST',
            path='/v2.0/tokens',
            body={
                'auth': {
                    'passwordCredentials': {
                        'userId': self.default_domain_user['id'],
                        'password': self.default_domain_user['password']
                    }
                }
            })
        v2_token_data = r.result
        v2_token = v2_token_data['access']['token']['id']

        r = self.get('/auth/tokens', headers={'X-Subject-Token': v2_token})
        self.assertValidUnscopedTokenResponse(r)
        v3_token_data = r.result

        self.assertEqual(v2_token_data['access']['user']['id'],
                         v3_token_data['token']['user']['id'])
        self.assertTimestampEqual(v2_token_data['access']['token']['expires'],
                                  v3_token_data['token']['expires_at'])

    def test_v2_v3_token_intermix(self):
        r = self.admin_request(
            path='/v2.0/tokens',
            method='POST',
            body={
                'auth': {
                    'passwordCredentials': {
                        'userId': self.default_domain_user['id'],
                        'password': self.default_domain_user['password']
                    },
                    'tenantId': self.default_domain_project['id']
                }
            })
        v2_token_data = r.result
        v2_token = v2_token_data['access']['token']['id']

        r = self.get('/auth/tokens', headers={'X-Subject-Token': v2_token})
        self.assertValidProjectScopedTokenResponse(r)
        v3_token_data = r.result

        self.assertEqual(v2_token_data['access']['user']['id'],
                         v3_token_data['token']['user']['id'])
        self.assertTimestampEqual(v2_token_data['access']['token']['expires'],
                                  v3_token_data['token']['expires_at'])
        self.assertEqual(v2_token_data['access']['user']['roles'][0]['name'],
                         v3_token_data['token']['roles'][0]['name'])

        v2_issued_at = timeutils.parse_isotime(
            v2_token_data['access']['token']['issued_at'])
        v3_issued_at = timeutils.parse_isotime(
            v3_token_data['token']['issued_at'])

        self.assertEqual(v2_issued_at, v3_issued_at)

    def test_create_v3_project_token_from_v2_project_token(self):
        r = self.admin_request(
            path='/v2.0/tokens',
            method='POST',
            body={
                'auth': {
                    'passwordCredentials': {
                        'userId': self.default_domain_user['id'],
                        'password': self.default_domain_user['password']
                    },
                    'tenantId': self.default_domain_project['id']
                }
            })
        v2_token_data = r.result
        v2_token = v2_token_data['access']['token']['id']

        auth_data = self.build_authentication_request(
            token=v2_token,
            project_id=self.default_domain_project['id'])
        r = self.v3_create_token(auth_data)
        self.assertValidScopedTokenResponse(r)

    def test_v2_token_deleted_on_v3(self):
        # Create a v2 token.
        body = {
            'auth': {
                'passwordCredentials': {
                    'userId': self.default_domain_user['id'],
                    'password': self.default_domain_user['password']
                },
                'tenantId': self.default_domain_project['id']
            }
        }
        r = self.admin_request(
            path='/v2.0/tokens', method='POST', body=body)
        v2_token = r.result['access']['token']['id']

        # Delete the v2 token using v3.
        self.delete(
            '/auth/tokens', headers={'X-Subject-Token': v2_token})

        # Attempting to use the deleted token on v2 should fail.
        self.admin_request(
            path='/v2.0/tenants', method='GET', token=v2_token,
            expected_status=http_client.UNAUTHORIZED)

    def test_rescoping_token(self):
        expires = self.v3_token_data['token']['expires_at']

        # rescope the token
        r = self.v3_create_token(self.build_authentication_request(
            token=self.v3_token,
            project_id=self.project_id))
        self.assertValidProjectScopedTokenResponse(r)

        # ensure token expiration stayed the same
        self.assertTimestampEqual(expires, r.result['token']['expires_at'])

    def test_check_token(self):
        self.head('/auth/tokens', headers=self.headers,
                  expected_status=http_client.OK)

    def test_validate_token(self):
        r = self.get('/auth/tokens', headers=self.headers)
        self.assertValidUnscopedTokenResponse(r)

    def test_validate_missing_subject_token(self):
        self.get('/auth/tokens',
                 expected_status=http_client.NOT_FOUND)

    def test_validate_missing_auth_token(self):
        self.admin_request(
            method='GET',
            path='/v3/projects',
            token=None,
            expected_status=http_client.UNAUTHORIZED)

    def test_validate_token_nocatalog(self):
        v3_token = self.get_requested_token(self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id']))
        r = self.get(
            '/auth/tokens?nocatalog',
            headers={'X-Subject-Token': v3_token})
        self.assertValidProjectScopedTokenResponse(r, require_catalog=False)

    def test_is_admin_token_by_ids(self):
        self.config_fixture.config(
            group='resource',
            admin_project_domain_name=self.domain['name'],
            admin_project_name=self.project['name'])
        r = self.v3_create_token(self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id']))
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=True)
        v3_token = r.headers.get('X-Subject-Token')
        r = self.get('/auth/tokens', headers={'X-Subject-Token': v3_token})
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=True)

    def test_is_admin_token_by_names(self):
        self.config_fixture.config(
            group='resource',
            admin_project_domain_name=self.domain['name'],
            admin_project_name=self.project['name'])
        r = self.v3_create_token(self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_domain_name=self.domain['name'],
            project_name=self.project['name']))
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=True)
        v3_token = r.headers.get('X-Subject-Token')
        r = self.get('/auth/tokens', headers={'X-Subject-Token': v3_token})
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=True)

    def test_token_for_non_admin_project_is_not_admin(self):
        self.config_fixture.config(
            group='resource',
            admin_project_domain_name=self.domain['name'],
            admin_project_name=uuid.uuid4().hex)
        r = self.v3_create_token(self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id']))
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=False)
        v3_token = r.headers.get('X-Subject-Token')
        r = self.get('/auth/tokens', headers={'X-Subject-Token': v3_token})
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=False)

    def test_token_for_non_admin_domain_same_project_name_is_not_admin(self):
        self.config_fixture.config(
            group='resource',
            admin_project_domain_name=uuid.uuid4().hex,
            admin_project_name=self.project['name'])
        r = self.v3_create_token(self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id']))
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=False)
        v3_token = r.headers.get('X-Subject-Token')
        r = self.get('/auth/tokens', headers={'X-Subject-Token': v3_token})
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=False)

    def test_only_admin_project_set_acts_as_non_admin(self):
        self.config_fixture.config(
            group='resource',
            admin_project_name=self.project['name'])
        r = self.v3_create_token(self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id']))
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=None)
        v3_token = r.headers.get('X-Subject-Token')
        r = self.get('/auth/tokens', headers={'X-Subject-Token': v3_token})
        self.assertValidProjectScopedTokenResponse(r, is_admin_project=None)

    def _create_role(self, domain_id=None):
        """Call ``POST /roles``."""
        ref = unit.new_role_ref(domain_id=domain_id)
        r = self.post('/roles', body={'role': ref})
        return self.assertValidRoleResponse(r, ref)

    def _create_implied_role(self, prior_id):
        implied = self._create_role()
        url = '/roles/%s/implies/%s' % (prior_id, implied['id'])
        self.put(url, expected_status=http_client.CREATED)
        return implied

    def _delete_implied_role(self, prior_role_id, implied_role_id):
        url = '/roles/%s/implies/%s' % (prior_role_id, implied_role_id)
        self.delete(url)

    def _get_scoped_token_roles(self, is_domain=False):
        if is_domain:
            v3_token = self.get_domain_scoped_token()
        else:
            v3_token = self.get_scoped_token()

        r = self.get('/auth/tokens', headers={'X-Subject-Token': v3_token})
        v3_token_data = r.result
        token_roles = v3_token_data['token']['roles']
        return token_roles

    def _create_implied_role_shows_in_v3_token(self, is_domain):
        token_roles = self._get_scoped_token_roles(is_domain)
        self.assertEqual(1, len(token_roles))

        prior = token_roles[0]['id']
        implied1 = self._create_implied_role(prior)

        token_roles = self._get_scoped_token_roles(is_domain)
        self.assertEqual(2, len(token_roles))

        implied2 = self._create_implied_role(prior)
        token_roles = self._get_scoped_token_roles(is_domain)
        self.assertEqual(3, len(token_roles))

        token_role_ids = [role['id'] for role in token_roles]
        self.assertIn(prior, token_role_ids)
        self.assertIn(implied1['id'], token_role_ids)
        self.assertIn(implied2['id'], token_role_ids)

    def test_create_implied_role_shows_in_v3_project_token(self):
        # regardless of the default chosen, this should always
        # test with the option set.
        self.config_fixture.config(group='token', infer_roles=True)
        self._create_implied_role_shows_in_v3_token(False)

    def test_create_implied_role_shows_in_v3_domain_token(self):
        self.config_fixture.config(group='token', infer_roles=True)
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domain['id'])

        self._create_implied_role_shows_in_v3_token(True)

    def test_group_assigned_implied_role_shows_in_v3_token(self):
        self.config_fixture.config(group='token', infer_roles=True)
        is_domain = False
        token_roles = self._get_scoped_token_roles(is_domain)
        self.assertEqual(1, len(token_roles))

        new_role = self._create_role()
        prior = new_role['id']

        new_group_ref = unit.new_group_ref(domain_id=self.domain['id'])
        new_group = self.identity_api.create_group(new_group_ref)
        self.assignment_api.create_grant(prior,
                                         group_id=new_group['id'],
                                         project_id=self.project['id'])

        token_roles = self._get_scoped_token_roles(is_domain)
        self.assertEqual(1, len(token_roles))

        self.identity_api.add_user_to_group(self.user['id'],
                                            new_group['id'])

        token_roles = self._get_scoped_token_roles(is_domain)
        self.assertEqual(2, len(token_roles))

        implied1 = self._create_implied_role(prior)

        token_roles = self._get_scoped_token_roles(is_domain)
        self.assertEqual(3, len(token_roles))

        implied2 = self._create_implied_role(prior)
        token_roles = self._get_scoped_token_roles(is_domain)
        self.assertEqual(4, len(token_roles))

        token_role_ids = [role['id'] for role in token_roles]
        self.assertIn(prior, token_role_ids)
        self.assertIn(implied1['id'], token_role_ids)
        self.assertIn(implied2['id'], token_role_ids)

    def test_multiple_implied_roles_show_in_v3_token(self):
        self.config_fixture.config(group='token', infer_roles=True)
        token_roles = self._get_scoped_token_roles()
        self.assertEqual(1, len(token_roles))

        prior = token_roles[0]['id']
        implied1 = self._create_implied_role(prior)
        implied2 = self._create_implied_role(prior)
        implied3 = self._create_implied_role(prior)

        token_roles = self._get_scoped_token_roles()
        self.assertEqual(4, len(token_roles))

        token_role_ids = [role['id'] for role in token_roles]
        self.assertIn(prior, token_role_ids)
        self.assertIn(implied1['id'], token_role_ids)
        self.assertIn(implied2['id'], token_role_ids)
        self.assertIn(implied3['id'], token_role_ids)

    def test_chained_implied_role_shows_in_v3_token(self):
        self.config_fixture.config(group='token', infer_roles=True)
        token_roles = self._get_scoped_token_roles()
        self.assertEqual(1, len(token_roles))

        prior = token_roles[0]['id']
        implied1 = self._create_implied_role(prior)
        implied2 = self._create_implied_role(implied1['id'])
        implied3 = self._create_implied_role(implied2['id'])

        token_roles = self._get_scoped_token_roles()
        self.assertEqual(4, len(token_roles))

        token_role_ids = [role['id'] for role in token_roles]

        self.assertIn(prior, token_role_ids)
        self.assertIn(implied1['id'], token_role_ids)
        self.assertIn(implied2['id'], token_role_ids)
        self.assertIn(implied3['id'], token_role_ids)

    def test_implied_role_disabled_by_config(self):
        self.config_fixture.config(group='token', infer_roles=False)
        token_roles = self._get_scoped_token_roles()
        self.assertEqual(1, len(token_roles))

        prior = token_roles[0]['id']
        implied1 = self._create_implied_role(prior)
        implied2 = self._create_implied_role(implied1['id'])
        self._create_implied_role(implied2['id'])

        token_roles = self._get_scoped_token_roles()
        self.assertEqual(1, len(token_roles))
        token_role_ids = [role['id'] for role in token_roles]
        self.assertIn(prior, token_role_ids)

    def test_delete_implied_role_do_not_show_in_v3_token(self):
        self.config_fixture.config(group='token', infer_roles=True)
        token_roles = self._get_scoped_token_roles()
        prior = token_roles[0]['id']
        implied = self._create_implied_role(prior)

        token_roles = self._get_scoped_token_roles()
        self.assertEqual(2, len(token_roles))
        self._delete_implied_role(prior, implied['id'])

        token_roles = self._get_scoped_token_roles()
        self.assertEqual(1, len(token_roles))

    def test_unrelated_implied_roles_do_not_change_v3_token(self):
        self.config_fixture.config(group='token', infer_roles=True)
        token_roles = self._get_scoped_token_roles()
        prior = token_roles[0]['id']
        implied = self._create_implied_role(prior)

        token_roles = self._get_scoped_token_roles()
        self.assertEqual(2, len(token_roles))

        unrelated = self._create_role()
        url = '/roles/%s/implies/%s' % (unrelated['id'], implied['id'])
        self.put(url, expected_status=http_client.CREATED)

        token_roles = self._get_scoped_token_roles()
        self.assertEqual(2, len(token_roles))

        self._delete_implied_role(unrelated['id'], implied['id'])
        token_roles = self._get_scoped_token_roles()
        self.assertEqual(2, len(token_roles))

    def test_domain_specific_roles_do_not_show_v3_token(self):
        self.config_fixture.config(group='token', infer_roles=True)
        initial_token_roles = self._get_scoped_token_roles()

        new_role = self._create_role(domain_id=self.domain_id)
        self.assignment_api.create_grant(new_role['id'],
                                         user_id=self.user['id'],
                                         project_id=self.project['id'])
        implied = self._create_implied_role(new_role['id'])

        token_roles = self._get_scoped_token_roles()
        self.assertEqual(len(initial_token_roles) + 1, len(token_roles))

        # The implied role from the domain specific role should be in the
        # token, but not the domain specific role itself.
        token_role_ids = [role['id'] for role in token_roles]
        self.assertIn(implied['id'], token_role_ids)
        self.assertNotIn(new_role['id'], token_role_ids)

    def test_remove_all_roles_from_scope_result_in_404(self):
        # create a new user
        new_user = unit.create_user(self.identity_api,
                                    domain_id=self.domain['id'])

        # give the new user a role on a project
        path = '/projects/%s/users/%s/roles/%s' % (
            self.project['id'], new_user['id'], self.role['id'])
        self.put(path=path)

        # authenticate as the new user and get a project-scoped token
        auth_data = self.build_authentication_request(
            user_id=new_user['id'],
            password=new_user['password'],
            project_id=self.project['id'])
        subject_token_id = self.v3_create_token(auth_data).headers.get(
            'X-Subject-Token')

        # make sure the project-scoped token is valid
        headers = {'X-Subject-Token': subject_token_id}
        r = self.get('/auth/tokens', headers=headers)
        self.assertValidProjectScopedTokenResponse(r)

        # remove the roles from the user for the given scope
        path = '/projects/%s/users/%s/roles/%s' % (
            self.project['id'], new_user['id'], self.role['id'])
        self.delete(path=path)

        # token validation should now result in 404
        self.get('/auth/tokens', headers=headers,
                 expected_status=http_client.NOT_FOUND)

    def test_create_token_with_nonexistant_user_id_fails(self):
        auth_data = self.build_authentication_request(
            user_id=uuid.uuid4().hex,
            password=self.user['password'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_token_with_nonexistant_username_fails(self):
        auth_data = self.build_authentication_request(
            username=uuid.uuid4().hex,
            user_domain_id=self.domain['id'],
            password=self.user['password'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_token_with_nonexistant_domain_id_fails(self):
        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_id=uuid.uuid4().hex,
            password=self.user['password'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_token_with_nonexistant_domain_name_fails(self):
        auth_data = self.build_authentication_request(
            username=self.user['name'],
            user_domain_name=uuid.uuid4().hex,
            password=self.user['password'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_token_with_wrong_password_fails(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=uuid.uuid4().hex)
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_user_and_group_roles_scoped_token(self):
        """Test correct roles are returned in scoped token.

        Test Plan:

        - Create a domain, with 1 project, 2 users (user1 and user2)
          and 2 groups (group1 and group2)
        - Make user1 a member of group1, user2 a member of group2
        - Create 8 roles, assigning them to each of the 8 combinations
          of users/groups on domain/project
        - Get a project scoped token for user1, checking that the right
          two roles are returned (one directly assigned, one by virtue
          of group membership)
        - Repeat this for a domain scoped token
        - Make user1 also a member of group2
        - Get another scoped token making sure the additional role
          shows up
        - User2 is just here as a spoiler, to make sure we don't get
          any roles uniquely assigned to it returned in any of our
          tokens

        """
        domainA = unit.new_domain_ref()
        self.resource_api.create_domain(domainA['id'], domainA)
        projectA = unit.new_project_ref(domain_id=domainA['id'])
        self.resource_api.create_project(projectA['id'], projectA)

        user1 = unit.create_user(self.identity_api, domain_id=domainA['id'])

        user2 = unit.create_user(self.identity_api, domain_id=domainA['id'])

        group1 = unit.new_group_ref(domain_id=domainA['id'])
        group1 = self.identity_api.create_group(group1)

        group2 = unit.new_group_ref(domain_id=domainA['id'])
        group2 = self.identity_api.create_group(group2)

        self.identity_api.add_user_to_group(user1['id'],
                                            group1['id'])
        self.identity_api.add_user_to_group(user2['id'],
                                            group2['id'])

        # Now create all the roles and assign them
        role_list = []
        for _ in range(8):
            role = unit.new_role_ref()
            self.role_api.create_role(role['id'], role)
            role_list.append(role)

        self.assignment_api.create_grant(role_list[0]['id'],
                                         user_id=user1['id'],
                                         domain_id=domainA['id'])
        self.assignment_api.create_grant(role_list[1]['id'],
                                         user_id=user1['id'],
                                         project_id=projectA['id'])
        self.assignment_api.create_grant(role_list[2]['id'],
                                         user_id=user2['id'],
                                         domain_id=domainA['id'])
        self.assignment_api.create_grant(role_list[3]['id'],
                                         user_id=user2['id'],
                                         project_id=projectA['id'])
        self.assignment_api.create_grant(role_list[4]['id'],
                                         group_id=group1['id'],
                                         domain_id=domainA['id'])
        self.assignment_api.create_grant(role_list[5]['id'],
                                         group_id=group1['id'],
                                         project_id=projectA['id'])
        self.assignment_api.create_grant(role_list[6]['id'],
                                         group_id=group2['id'],
                                         domain_id=domainA['id'])
        self.assignment_api.create_grant(role_list[7]['id'],
                                         group_id=group2['id'],
                                         project_id=projectA['id'])

        # First, get a project scoped token - which should
        # contain the direct user role and the one by virtue
        # of group membership
        auth_data = self.build_authentication_request(
            user_id=user1['id'],
            password=user1['password'],
            project_id=projectA['id'])
        r = self.v3_create_token(auth_data)
        token = self.assertValidScopedTokenResponse(r)
        roles_ids = []
        for ref in token['roles']:
            roles_ids.append(ref['id'])
        self.assertEqual(2, len(token['roles']))
        self.assertIn(role_list[1]['id'], roles_ids)
        self.assertIn(role_list[5]['id'], roles_ids)

        # Now the same thing for a domain scoped token
        auth_data = self.build_authentication_request(
            user_id=user1['id'],
            password=user1['password'],
            domain_id=domainA['id'])
        r = self.v3_create_token(auth_data)
        token = self.assertValidScopedTokenResponse(r)
        roles_ids = []
        for ref in token['roles']:
            roles_ids.append(ref['id'])
        self.assertEqual(2, len(token['roles']))
        self.assertIn(role_list[0]['id'], roles_ids)
        self.assertIn(role_list[4]['id'], roles_ids)

        # Finally, add user1 to the 2nd group, and get a new
        # scoped token - the extra role should now be included
        # by virtue of the 2nd group
        self.identity_api.add_user_to_group(user1['id'],
                                            group2['id'])
        auth_data = self.build_authentication_request(
            user_id=user1['id'],
            password=user1['password'],
            project_id=projectA['id'])
        r = self.v3_create_token(auth_data)
        token = self.assertValidScopedTokenResponse(r)
        roles_ids = []
        for ref in token['roles']:
            roles_ids.append(ref['id'])
        self.assertEqual(3, len(token['roles']))
        self.assertIn(role_list[1]['id'], roles_ids)
        self.assertIn(role_list[5]['id'], roles_ids)
        self.assertIn(role_list[7]['id'], roles_ids)

    def test_auth_token_cross_domain_group_and_project(self):
        """Verify getting a token in cross domain group/project roles."""
        # create domain, project and group and grant roles to user
        domain1 = unit.new_domain_ref()
        self.resource_api.create_domain(domain1['id'], domain1)
        project1 = unit.new_project_ref(domain_id=domain1['id'])
        self.resource_api.create_project(project1['id'], project1)
        user_foo = unit.create_user(self.identity_api,
                                    domain_id=test_v3.DEFAULT_DOMAIN_ID)
        role_member = unit.new_role_ref()
        self.role_api.create_role(role_member['id'], role_member)
        role_admin = unit.new_role_ref()
        self.role_api.create_role(role_admin['id'], role_admin)
        role_foo_domain1 = unit.new_role_ref()
        self.role_api.create_role(role_foo_domain1['id'], role_foo_domain1)
        role_group_domain1 = unit.new_role_ref()
        self.role_api.create_role(role_group_domain1['id'], role_group_domain1)
        self.assignment_api.add_user_to_project(project1['id'],
                                                user_foo['id'])
        new_group = unit.new_group_ref(domain_id=domain1['id'])
        new_group = self.identity_api.create_group(new_group)
        self.identity_api.add_user_to_group(user_foo['id'],
                                            new_group['id'])
        self.assignment_api.create_grant(
            user_id=user_foo['id'],
            project_id=project1['id'],
            role_id=role_member['id'])
        self.assignment_api.create_grant(
            group_id=new_group['id'],
            project_id=project1['id'],
            role_id=role_admin['id'])
        self.assignment_api.create_grant(
            user_id=user_foo['id'],
            domain_id=domain1['id'],
            role_id=role_foo_domain1['id'])
        self.assignment_api.create_grant(
            group_id=new_group['id'],
            domain_id=domain1['id'],
            role_id=role_group_domain1['id'])

        # Get a scoped token for the project
        auth_data = self.build_authentication_request(
            username=user_foo['name'],
            user_domain_id=test_v3.DEFAULT_DOMAIN_ID,
            password=user_foo['password'],
            project_name=project1['name'],
            project_domain_id=domain1['id'])

        r = self.v3_create_token(auth_data)
        scoped_token = self.assertValidScopedTokenResponse(r)
        project = scoped_token["project"]
        roles_ids = []
        for ref in scoped_token['roles']:
            roles_ids.append(ref['id'])
        self.assertEqual(project1['id'], project["id"])
        self.assertIn(role_member['id'], roles_ids)
        self.assertIn(role_admin['id'], roles_ids)
        self.assertNotIn(role_foo_domain1['id'], roles_ids)
        self.assertNotIn(role_group_domain1['id'], roles_ids)

    def test_remote_user_no_realm(self):
        api = auth.controllers.Auth()
        context, auth_info, auth_context = self.build_external_auth_request(
            self.default_domain_user['name'])
        api.authenticate(context, auth_info, auth_context)
        self.assertEqual(self.default_domain_user['id'],
                         auth_context['user_id'])
        # Now test to make sure the user name can, itself, contain the
        # '@' character.
        user = {'name': 'myname@mydivision'}
        self.identity_api.update_user(self.default_domain_user['id'], user)
        context, auth_info, auth_context = self.build_external_auth_request(
            user["name"])
        api.authenticate(context, auth_info, auth_context)
        self.assertEqual(self.default_domain_user['id'],
                         auth_context['user_id'])

    def test_remote_user_no_domain(self):
        api = auth.controllers.Auth()
        context, auth_info, auth_context = self.build_external_auth_request(
            self.user['name'])
        self.assertRaises(exception.Unauthorized,
                          api.authenticate,
                          context,
                          auth_info,
                          auth_context)

    def test_remote_user_and_password(self):
        # both REMOTE_USER and password methods must pass.
        # note that they do not have to match
        api = auth.controllers.Auth()
        auth_data = self.build_authentication_request(
            user_domain_id=self.default_domain_user['domain_id'],
            username=self.default_domain_user['name'],
            password=self.default_domain_user['password'])['auth']
        context, auth_info, auth_context = self.build_external_auth_request(
            self.default_domain_user['name'], auth_data=auth_data)

        api.authenticate(context, auth_info, auth_context)

    def test_remote_user_and_explicit_external(self):
        # both REMOTE_USER and password methods must pass.
        # note that they do not have to match
        auth_data = self.build_authentication_request(
            user_domain_id=self.domain['id'],
            username=self.user['name'],
            password=self.user['password'])['auth']
        auth_data['identity']['methods'] = ["password", "external"]
        auth_data['identity']['external'] = {}
        api = auth.controllers.Auth()
        auth_info = auth.core.AuthInfo(auth_data)
        auth_context = auth.core.AuthContext(extras={}, methods=[])
        self.assertRaises(exception.Unauthorized,
                          api.authenticate,
                          self.make_request(),
                          auth_info,
                          auth_context)

    def test_remote_user_bad_password(self):
        # both REMOTE_USER and password methods must pass.
        api = auth.controllers.Auth()
        auth_data = self.build_authentication_request(
            user_domain_id=self.domain['id'],
            username=self.user['name'],
            password='badpassword')['auth']
        context, auth_info, auth_context = self.build_external_auth_request(
            self.default_domain_user['name'], auth_data=auth_data)
        self.assertRaises(exception.Unauthorized,
                          api.authenticate,
                          context,
                          auth_info,
                          auth_context)

    def test_bind_not_set_with_remote_user(self):
        self.config_fixture.config(group='token', bind=[])
        auth_data = self.build_authentication_request()
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})
        r = self.v3_create_token(auth_data)
        token = self.assertValidUnscopedTokenResponse(r)
        self.assertNotIn('bind', token)

    def test_verify_with_bound_token(self):
        self.config_fixture.config(group='token', bind='kerberos')
        auth_data = self.build_authentication_request(
            project_id=self.project['id'])
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})

        token = self.get_requested_token(auth_data)
        headers = {'X-Subject-Token': token}
        r = self.get('/auth/tokens', headers=headers, token=token)
        token = self.assertValidProjectScopedTokenResponse(r)
        self.assertEqual(self.default_domain_user['name'],
                         token['bind']['kerberos'])

    def test_auth_with_bind_token(self):
        self.config_fixture.config(group='token', bind=['kerberos'])

        auth_data = self.build_authentication_request()
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})
        r = self.v3_create_token(auth_data)

        # the unscoped token should have bind information in it
        token = self.assertValidUnscopedTokenResponse(r)
        self.assertEqual(remote_user, token['bind']['kerberos'])

        token = r.headers.get('X-Subject-Token')

        # using unscoped token with remote user succeeds
        auth_params = {'token': token, 'project_id': self.project_id}
        auth_data = self.build_authentication_request(**auth_params)
        r = self.v3_create_token(auth_data)
        token = self.assertValidProjectScopedTokenResponse(r)

        # the bind information should be carried over from the original token
        self.assertEqual(remote_user, token['bind']['kerberos'])

    def test_v2_v3_bind_token_intermix(self):
        self.config_fixture.config(group='token', bind='kerberos')

        # we need our own user registered to the default domain because of
        # the way external auth works.
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})
        body = {'auth': {}}
        resp = self.admin_request(path='/v2.0/tokens',
                                  method='POST',
                                  body=body)

        v2_token_data = resp.result

        bind = v2_token_data['access']['token']['bind']
        self.assertEqual(self.default_domain_user['name'], bind['kerberos'])

        v2_token_id = v2_token_data['access']['token']['id']
        # NOTE(gyee): self.get() will try to obtain an auth token if one
        # is not provided. When REMOTE_USER is present in the request
        # environment, the external user auth plugin is used in conjunction
        # with the password auth for the admin user. Therefore, we need to
        # cleanup the REMOTE_USER information from the previous call.
        del self.admin_app.extra_environ['REMOTE_USER']
        headers = {'X-Subject-Token': v2_token_id}
        resp = self.get('/auth/tokens', headers=headers)
        token_data = resp.result

        self.assertDictEqual(v2_token_data['access']['token']['bind'],
                             token_data['token']['bind'])

    def test_fetch_expired_allow_expired(self):
        self.config_fixture.config(group='token',
                                   expiration=10,
                                   allow_expired_window=20)
        time = datetime.datetime.utcnow()
        with freezegun.freeze_time(time) as frozen_datetime:
            token = self._get_project_scoped_token()

            # initially it validates because it's within time
            frozen_datetime.tick(delta=datetime.timedelta(seconds=2))
            self._validate_token(token)

            # after passing expiry time validation fails
            frozen_datetime.tick(delta=datetime.timedelta(seconds=12))
            self._validate_token(token, expected_status=http_client.NOT_FOUND)

            # flush the tokens, this will only have an effect on sql
            try:
                self.token_provider_api._persistence.flush_expired_tokens()
            except exception.NotImplemented:
                pass

            # but if we pass allow_expired it validates
            self._validate_token(token, allow_expired=True)

            # and then if we're passed the allow_expired_window it will fail
            # anyway raises expired when now > expiration + window
            frozen_datetime.tick(delta=datetime.timedelta(seconds=22))
            self._validate_token(token,
                                 allow_expired=True,
                                 expected_status=http_client.NOT_FOUND)


class TokenDataTests(object):
    """Test the data in specific token types."""

    def test_unscoped_token_format(self):
        # ensure the unscoped token response contains the appropriate data
        r = self.get('/auth/tokens', headers=self.headers)
        self.assertValidUnscopedTokenResponse(r)

    def test_domain_scoped_token_format(self):
        # ensure the domain scoped token response contains the appropriate data
        self.assignment_api.create_grant(
            self.role['id'],
            user_id=self.default_domain_user['id'],
            domain_id=self.domain['id'])

        domain_scoped_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.default_domain_user['id'],
                password=self.default_domain_user['password'],
                domain_id=self.domain['id'])
        )
        self.headers['X-Subject-Token'] = domain_scoped_token
        r = self.get('/auth/tokens', headers=self.headers)
        self.assertValidDomainScopedTokenResponse(r)

    def test_project_scoped_token_format(self):
        # ensure project scoped token responses contains the appropriate data
        project_scoped_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.default_domain_user['id'],
                password=self.default_domain_user['password'],
                project_id=self.default_domain_project['id'])
        )
        self.headers['X-Subject-Token'] = project_scoped_token
        r = self.get('/auth/tokens', headers=self.headers)
        self.assertValidProjectScopedTokenResponse(r)

    def test_extra_data_in_unscoped_token_fails_validation(self):
        # ensure unscoped token response contains the appropriate data
        r = self.get('/auth/tokens', headers=self.headers)

        # populate the response result with some extra data
        r.result['token'][u'extra'] = six.text_type(uuid.uuid4().hex)
        self.assertRaises(exception.SchemaValidationError,
                          self.assertValidUnscopedTokenResponse,
                          r)

    def test_extra_data_in_domain_scoped_token_fails_validation(self):
        # ensure domain scoped token response contains the appropriate data
        self.assignment_api.create_grant(
            self.role['id'],
            user_id=self.default_domain_user['id'],
            domain_id=self.domain['id'])

        domain_scoped_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.default_domain_user['id'],
                password=self.default_domain_user['password'],
                domain_id=self.domain['id'])
        )
        self.headers['X-Subject-Token'] = domain_scoped_token
        r = self.get('/auth/tokens', headers=self.headers)

        # populate the response result with some extra data
        r.result['token'][u'extra'] = six.text_type(uuid.uuid4().hex)
        self.assertRaises(exception.SchemaValidationError,
                          self.assertValidDomainScopedTokenResponse,
                          r)

    def test_extra_data_in_project_scoped_token_fails_validation(self):
        # ensure project scoped token responses contains the appropriate data
        project_scoped_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.default_domain_user['id'],
                password=self.default_domain_user['password'],
                project_id=self.default_domain_project['id'])
        )
        self.headers['X-Subject-Token'] = project_scoped_token
        resp = self.get('/auth/tokens', headers=self.headers)

        # populate the response result with some extra data
        resp.result['token'][u'extra'] = six.text_type(uuid.uuid4().hex)
        self.assertRaises(exception.SchemaValidationError,
                          self.assertValidProjectScopedTokenResponse,
                          resp)


class AllowRescopeScopedTokenDisabledTests(test_v3.RestfulTestCase):
    def config_overrides(self):
        super(AllowRescopeScopedTokenDisabledTests, self).config_overrides()
        self.config_fixture.config(
            group='token',
            allow_rescope_scoped_token=False)

    def test_rescoping_v3_to_v3_disabled(self):
        self.v3_create_token(
            self.build_authentication_request(
                token=self.get_scoped_token(),
                project_id=self.project_id),
            expected_status=http_client.FORBIDDEN)

    def _v2_token(self):
        body = {
            'auth': {
                "tenantId": self.default_domain_project['id'],
                'passwordCredentials': {
                    'userId': self.default_domain_user['id'],
                    'password': self.default_domain_user['password']
                }
            }}
        resp = self.admin_request(path='/v2.0/tokens',
                                  method='POST',
                                  body=body)
        v2_token_data = resp.result
        return v2_token_data

    def _v2_token_from_token(self, token):
        body = {
            'auth': {
                "tenantId": self.project['id'],
                "token": token
            }}
        self.admin_request(path='/v2.0/tokens',
                           method='POST',
                           body=body,
                           expected_status=http_client.FORBIDDEN)

    def test_rescoping_v2_to_v3_disabled(self):
        token = self._v2_token()
        self.v3_create_token(
            self.build_authentication_request(
                token=token['access']['token']['id'],
                project_id=self.project_id),
            expected_status=http_client.FORBIDDEN)

    def test_rescoping_v3_to_v2_disabled(self):
        token = {'id': self.get_scoped_token()}
        self._v2_token_from_token(token)

    def test_rescoping_v2_to_v2_disabled(self):
        token = self._v2_token()
        self._v2_token_from_token(token['access']['token'])

    def test_rescoped_domain_token_disabled(self):

        self.domainA = unit.new_domain_ref()
        self.resource_api.create_domain(self.domainA['id'], self.domainA)
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.user['id'],
                                         domain_id=self.domainA['id'])
        unscoped_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.user['id'],
                password=self.user['password']))
        # Get a domain-scoped token from the unscoped token
        domain_scoped_token = self.get_requested_token(
            self.build_authentication_request(
                token=unscoped_token,
                domain_id=self.domainA['id']))
        self.v3_create_token(
            self.build_authentication_request(
                token=domain_scoped_token,
                project_id=self.project_id),
            expected_status=http_client.FORBIDDEN)


class TestUUIDTokenAPIs(test_v3.RestfulTestCase, TokenAPITests,
                        TokenDataTests):
    def config_overrides(self):
        super(TestUUIDTokenAPIs, self).config_overrides()
        self.config_fixture.config(group='token', provider='uuid')

    def setUp(self):
        super(TestUUIDTokenAPIs, self).setUp()
        self.doSetUp()

    def test_v3_token_id(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'])
        resp = self.v3_create_token(auth_data)
        token_data = resp.result
        self.assertIn('expires_at', token_data['token'])


class TestFernetTokenAPIs(test_v3.RestfulTestCase, TokenAPITests,
                          TokenDataTests):
    def config_overrides(self):
        super(TestFernetTokenAPIs, self).config_overrides()
        self.config_fixture.config(group='token', provider='fernet',
                                   cache_on_issue=True)
        self.useFixture(
            ksfixtures.KeyRepository(
                self.config_fixture,
                'fernet_tokens',
                CONF.fernet_tokens.max_active_keys
            )
        )

    def setUp(self):
        super(TestFernetTokenAPIs, self).setUp()
        self.doSetUp()

    def _make_auth_request(self, auth_data):
        token = super(TestFernetTokenAPIs, self)._make_auth_request(auth_data)
        self.assertLess(len(token), 255)
        return token

    def test_validate_tampered_unscoped_token_fails(self):
        unscoped_token = self._get_unscoped_token()
        tampered_token = (unscoped_token[:50] + uuid.uuid4().hex +
                          unscoped_token[50 + 32:])
        self._validate_token(tampered_token,
                             expected_status=http_client.NOT_FOUND)

    def test_validate_tampered_project_scoped_token_fails(self):
        project_scoped_token = self._get_project_scoped_token()
        tampered_token = (project_scoped_token[:50] + uuid.uuid4().hex +
                          project_scoped_token[50 + 32:])
        self._validate_token(tampered_token,
                             expected_status=http_client.NOT_FOUND)

    def test_validate_tampered_trust_scoped_token_fails(self):
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Get a trust scoped token
        tampered_token = (trust_scoped_token[:50] + uuid.uuid4().hex +
                          trust_scoped_token[50 + 32:])
        self._validate_token(tampered_token,
                             expected_status=http_client.NOT_FOUND)

    def test_verify_with_bound_token(self):
        self.config_fixture.config(group='token', bind='kerberos')
        auth_data = self.build_authentication_request(
            project_id=self.project['id'])
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})
        # Bind not current supported by Fernet, see bug 1433311.
        self.v3_create_token(auth_data,
                             expected_status=http_client.NOT_IMPLEMENTED)

    def test_v2_v3_bind_token_intermix(self):
        self.config_fixture.config(group='token', bind='kerberos')

        # we need our own user registered to the default domain because of
        # the way external auth works.
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})
        body = {'auth': {}}
        # Bind not current supported by Fernet, see bug 1433311.
        self.admin_request(path='/v2.0/tokens',
                           method='POST',
                           body=body,
                           expected_status=http_client.NOT_IMPLEMENTED)

    def test_auth_with_bind_token(self):
        self.config_fixture.config(group='token', bind=['kerberos'])

        auth_data = self.build_authentication_request()
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})
        # Bind not current supported by Fernet, see bug 1433311.
        self.v3_create_token(auth_data,
                             expected_status=http_client.NOT_IMPLEMENTED)

    def test_trust_scoped_token_is_invalid_after_disabling_trustor(self):
        # NOTE(amakarov): have to override this test for non-persistent tokens
        # as TokenNotFound exception makes no sense for those.
        trustee_user, trust = self._create_trust()
        trust_scoped_token = self._get_trust_scoped_token(trustee_user, trust)
        # Validate a trust scoped token
        r = self._validate_token(trust_scoped_token)
        self.assertValidProjectScopedTokenResponse(r)

        # Disable the trustor
        trustor_update_ref = dict(enabled=False)
        self.identity_api.update_user(self.user['id'], trustor_update_ref)
        # Ensure validating a token for a disabled user fails
        self._validate_token(
            trust_scoped_token,
            expected_status=http_client.FORBIDDEN
        )


class TestTokenRevokeSelfAndAdmin(test_v3.RestfulTestCase):
    """Test token revoke using v3 Identity API by token owner and admin."""

    def load_sample_data(self):
        """Load Sample Data for Test Cases.

        Two domains, domainA and domainB
        Two users in domainA, userNormalA and userAdminA
        One user in domainB, userAdminB

        """
        super(TestTokenRevokeSelfAndAdmin, self).load_sample_data()
        # DomainA setup
        self.domainA = unit.new_domain_ref()
        self.resource_api.create_domain(self.domainA['id'], self.domainA)

        self.userAdminA = unit.create_user(self.identity_api,
                                           domain_id=self.domainA['id'])

        self.userNormalA = unit.create_user(self.identity_api,
                                            domain_id=self.domainA['id'])

        self.assignment_api.create_grant(self.role['id'],
                                         user_id=self.userAdminA['id'],
                                         domain_id=self.domainA['id'])

    def _policy_fixture(self):
        return ksfixtures.Policy(unit.dirs.etc('policy.v3cloudsample.json'),
                                 self.config_fixture)

    def test_user_revokes_own_token(self):
        user_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.userNormalA['id'],
                password=self.userNormalA['password'],
                user_domain_id=self.domainA['id']))
        self.assertNotEmpty(user_token)
        headers = {'X-Subject-Token': user_token}

        adminA_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.userAdminA['id'],
                password=self.userAdminA['password'],
                domain_name=self.domainA['name']))

        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.OK,
                  token=adminA_token)
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.OK,
                  token=user_token)
        self.delete('/auth/tokens', headers=headers,
                    token=user_token)
        # invalid X-Auth-Token and invalid X-Subject-Token
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.UNAUTHORIZED,
                  token=user_token)
        # invalid X-Auth-Token and invalid X-Subject-Token
        self.delete('/auth/tokens', headers=headers,
                    expected_status=http_client.UNAUTHORIZED,
                    token=user_token)
        # valid X-Auth-Token and invalid X-Subject-Token
        self.delete('/auth/tokens', headers=headers,
                    expected_status=http_client.NOT_FOUND,
                    token=adminA_token)
        # valid X-Auth-Token and invalid X-Subject-Token
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.NOT_FOUND,
                  token=adminA_token)

    def test_adminA_revokes_userA_token(self):
        user_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.userNormalA['id'],
                password=self.userNormalA['password'],
                user_domain_id=self.domainA['id']))
        self.assertNotEmpty(user_token)
        headers = {'X-Subject-Token': user_token}

        adminA_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.userAdminA['id'],
                password=self.userAdminA['password'],
                domain_name=self.domainA['name']))

        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.OK,
                  token=adminA_token)
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.OK,
                  token=user_token)
        self.delete('/auth/tokens', headers=headers,
                    token=adminA_token)
        # invalid X-Auth-Token and invalid X-Subject-Token
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.UNAUTHORIZED,
                  token=user_token)
        # valid X-Auth-Token and invalid X-Subject-Token
        self.delete('/auth/tokens', headers=headers,
                    expected_status=http_client.NOT_FOUND,
                    token=adminA_token)
        # valid X-Auth-Token and invalid X-Subject-Token
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.NOT_FOUND,
                  token=adminA_token)

    def test_adminB_fails_revoking_userA_token(self):
        # DomainB setup
        self.domainB = unit.new_domain_ref()
        self.resource_api.create_domain(self.domainB['id'], self.domainB)
        userAdminB = unit.create_user(self.identity_api,
                                      domain_id=self.domainB['id'])
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=userAdminB['id'],
                                         domain_id=self.domainB['id'])

        user_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.userNormalA['id'],
                password=self.userNormalA['password'],
                user_domain_id=self.domainA['id']))
        headers = {'X-Subject-Token': user_token}

        adminB_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=userAdminB['id'],
                password=userAdminB['password'],
                domain_name=self.domainB['name']))

        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.FORBIDDEN,
                  token=adminB_token)
        self.delete('/auth/tokens', headers=headers,
                    expected_status=http_client.FORBIDDEN,
                    token=adminB_token)


class TestTokenRevokeById(test_v3.RestfulTestCase):
    """Test token revocation on the v3 Identity API."""

    def config_overrides(self):
        super(TestTokenRevokeById, self).config_overrides()
        self.config_fixture.config(
            group='token',
            provider='fernet',
            revoke_by_id=False)
        self.useFixture(
            ksfixtures.KeyRepository(
                self.config_fixture,
                'fernet_tokens',
                CONF.fernet_tokens.max_active_keys
            )
        )

    def setUp(self):
        """Setup for Token Revoking Test Cases.

        As well as the usual housekeeping, create a set of domains,
        users, groups, roles and projects for the subsequent tests:

        - Two domains: A & B
        - Three users (1, 2 and 3)
        - Three groups (1, 2 and 3)
        - Two roles (1 and 2)
        - DomainA owns user1, domainB owns user2 and user3
        - DomainA owns group1 and group2, domainB owns group3
        - User1 and user2 are members of group1
        - User3 is a member of group2
        - Two projects: A & B, both in domainA
        - Group1 has role1 on Project A and B, meaning that user1 and user2
          will get these roles by virtue of membership
        - User1, 2 and 3 have role1 assigned to projectA
        - Group1 has role1 on Project A and B, meaning that user1 and user2
          will get role1 (duplicated) by virtue of membership
        - User1 has role2 assigned to domainA

        """
        super(TestTokenRevokeById, self).setUp()

        # Start by creating a couple of domains and projects
        self.domainA = unit.new_domain_ref()
        self.resource_api.create_domain(self.domainA['id'], self.domainA)
        self.domainB = unit.new_domain_ref()
        self.resource_api.create_domain(self.domainB['id'], self.domainB)
        self.projectA = unit.new_project_ref(domain_id=self.domainA['id'])
        self.resource_api.create_project(self.projectA['id'], self.projectA)
        self.projectB = unit.new_project_ref(domain_id=self.domainA['id'])
        self.resource_api.create_project(self.projectB['id'], self.projectB)

        # Now create some users
        self.user1 = unit.create_user(self.identity_api,
                                      domain_id=self.domainA['id'])

        self.user2 = unit.create_user(self.identity_api,
                                      domain_id=self.domainB['id'])

        self.user3 = unit.create_user(self.identity_api,
                                      domain_id=self.domainB['id'])

        self.group1 = unit.new_group_ref(domain_id=self.domainA['id'])
        self.group1 = self.identity_api.create_group(self.group1)

        self.group2 = unit.new_group_ref(domain_id=self.domainA['id'])
        self.group2 = self.identity_api.create_group(self.group2)

        self.group3 = unit.new_group_ref(domain_id=self.domainB['id'])
        self.group3 = self.identity_api.create_group(self.group3)

        self.identity_api.add_user_to_group(self.user1['id'],
                                            self.group1['id'])
        self.identity_api.add_user_to_group(self.user2['id'],
                                            self.group1['id'])
        self.identity_api.add_user_to_group(self.user3['id'],
                                            self.group2['id'])

        self.role1 = unit.new_role_ref()
        self.role_api.create_role(self.role1['id'], self.role1)
        self.role2 = unit.new_role_ref()
        self.role_api.create_role(self.role2['id'], self.role2)

        self.assignment_api.create_grant(self.role2['id'],
                                         user_id=self.user1['id'],
                                         domain_id=self.domainA['id'])
        self.assignment_api.create_grant(self.role1['id'],
                                         user_id=self.user1['id'],
                                         project_id=self.projectA['id'])
        self.assignment_api.create_grant(self.role1['id'],
                                         user_id=self.user2['id'],
                                         project_id=self.projectA['id'])
        self.assignment_api.create_grant(self.role1['id'],
                                         user_id=self.user3['id'],
                                         project_id=self.projectA['id'])
        self.assignment_api.create_grant(self.role1['id'],
                                         group_id=self.group1['id'],
                                         project_id=self.projectA['id'])

    def test_unscoped_token_remains_valid_after_role_assignment(self):
        unscoped_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.user1['id'],
                password=self.user1['password']))

        scoped_token = self.get_requested_token(
            self.build_authentication_request(
                token=unscoped_token,
                project_id=self.projectA['id']))

        # confirm both tokens are valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': unscoped_token},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': scoped_token},
                  expected_status=http_client.OK)

        # create a new role
        role = unit.new_role_ref()
        self.role_api.create_role(role['id'], role)

        # assign a new role
        self.put(
            '/projects/%(project_id)s/users/%(user_id)s/roles/%(role_id)s' % {
                'project_id': self.projectA['id'],
                'user_id': self.user1['id'],
                'role_id': role['id']})

        # both tokens should remain valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': unscoped_token},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': scoped_token},
                  expected_status=http_client.OK)

    def test_deleting_user_grant_revokes_token(self):
        """Test deleting a user grant revokes token.

        Test Plan:

        - Get a token for user, scoped to Project
        - Delete the grant user has on Project
        - Check token is no longer valid

        """
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id'])
        token = self.get_requested_token(auth_data)
        # Confirm token is valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.OK)
        # Delete the grant, which should invalidate the token
        grant_url = (
            '/projects/%(project_id)s/users/%(user_id)s/'
            'roles/%(role_id)s' % {
                'project_id': self.project['id'],
                'user_id': self.user['id'],
                'role_id': self.role['id']})
        self.delete(grant_url)
        self.head('/auth/tokens', token=token,
                  expected_status=http_client.UNAUTHORIZED)

    def role_data_fixtures(self):
        self.projectC = unit.new_project_ref(domain_id=self.domainA['id'])
        self.resource_api.create_project(self.projectC['id'], self.projectC)
        self.user4 = unit.create_user(self.identity_api,
                                      domain_id=self.domainB['id'])
        self.user5 = unit.create_user(self.identity_api,
                                      domain_id=self.domainA['id'])
        self.user6 = unit.create_user(self.identity_api,
                                      domain_id=self.domainA['id'])
        self.identity_api.add_user_to_group(self.user5['id'],
                                            self.group1['id'])
        self.assignment_api.create_grant(self.role1['id'],
                                         group_id=self.group1['id'],
                                         project_id=self.projectB['id'])
        self.assignment_api.create_grant(self.role2['id'],
                                         user_id=self.user4['id'],
                                         project_id=self.projectC['id'])
        self.assignment_api.create_grant(self.role1['id'],
                                         user_id=self.user6['id'],
                                         project_id=self.projectA['id'])
        self.assignment_api.create_grant(self.role1['id'],
                                         user_id=self.user6['id'],
                                         domain_id=self.domainA['id'])

    def test_deleting_role_revokes_token(self):
        """Test deleting a role revokes token.

        Add some additional test data, namely:

        - A third project (project C)
        - Three additional users - user4 owned by domainB and user5 and 6 owned
          by domainA (different domain ownership should not affect the test
          results, just provided to broaden test coverage)
        - User5 is a member of group1
        - Group1 gets an additional assignment - role1 on projectB as well as
          its existing role1 on projectA
        - User4 has role2 on Project C
        - User6 has role1 on projectA and domainA
        - This allows us to create 5 tokens by virtue of different types of
          role assignment:
          - user1, scoped to ProjectA by virtue of user role1 assignment
          - user5, scoped to ProjectB by virtue of group role1 assignment
          - user4, scoped to ProjectC by virtue of user role2 assignment
          - user6, scoped to ProjectA by virtue of user role1 assignment
          - user6, scoped to DomainA by virtue of user role1 assignment
        - role1 is then deleted
        - Check the tokens on Project A and B, and DomainA are revoked, but not
          the one for Project C

        """
        self.role_data_fixtures()

        # Now we are ready to start issuing requests
        auth_data = self.build_authentication_request(
            user_id=self.user1['id'],
            password=self.user1['password'],
            project_id=self.projectA['id'])
        tokenA = self.get_requested_token(auth_data)
        auth_data = self.build_authentication_request(
            user_id=self.user5['id'],
            password=self.user5['password'],
            project_id=self.projectB['id'])
        tokenB = self.get_requested_token(auth_data)
        auth_data = self.build_authentication_request(
            user_id=self.user4['id'],
            password=self.user4['password'],
            project_id=self.projectC['id'])
        tokenC = self.get_requested_token(auth_data)
        auth_data = self.build_authentication_request(
            user_id=self.user6['id'],
            password=self.user6['password'],
            project_id=self.projectA['id'])
        tokenD = self.get_requested_token(auth_data)
        auth_data = self.build_authentication_request(
            user_id=self.user6['id'],
            password=self.user6['password'],
            domain_id=self.domainA['id'])
        tokenE = self.get_requested_token(auth_data)
        # Confirm tokens are valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenA},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenB},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenC},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenD},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenE},
                  expected_status=http_client.OK)

        # Delete the role, which should invalidate the tokens
        role_url = '/roles/%s' % self.role1['id']
        self.delete(role_url)

        # Check the tokens that used role1 is invalid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenA},
                  expected_status=http_client.NOT_FOUND)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenB},
                  expected_status=http_client.NOT_FOUND)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenD},
                  expected_status=http_client.NOT_FOUND)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenE},
                  expected_status=http_client.NOT_FOUND)

        # ...but the one using role2 is still valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': tokenC},
                  expected_status=http_client.OK)

    def test_domain_user_role_assignment_maintains_token(self):
        """Test user-domain role assignment maintains existing token.

        Test Plan:

        - Get a token for user1, scoped to ProjectA
        - Create a grant for user1 on DomainB
        - Check token is still valid

        """
        auth_data = self.build_authentication_request(
            user_id=self.user1['id'],
            password=self.user1['password'],
            project_id=self.projectA['id'])
        token = self.get_requested_token(auth_data)
        # Confirm token is valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.OK)
        # Assign a role, which should not affect the token
        grant_url = (
            '/domains/%(domain_id)s/users/%(user_id)s/'
            'roles/%(role_id)s' % {
                'domain_id': self.domainB['id'],
                'user_id': self.user1['id'],
                'role_id': self.role1['id']})
        self.put(grant_url)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.OK)

    def test_disabling_project_revokes_token(self):
        token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.user3['id'],
                password=self.user3['password'],
                project_id=self.projectA['id']))

        # confirm token is valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.OK)

        # disable the project, which should invalidate the token
        self.patch(
            '/projects/%(project_id)s' % {'project_id': self.projectA['id']},
            body={'project': {'enabled': False}})

        # user should no longer have access to the project
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.NOT_FOUND)
        self.v3_create_token(
            self.build_authentication_request(
                user_id=self.user3['id'],
                password=self.user3['password'],
                project_id=self.projectA['id']),
            expected_status=http_client.UNAUTHORIZED)

    def test_deleting_project_revokes_token(self):
        token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.user3['id'],
                password=self.user3['password'],
                project_id=self.projectA['id']))

        # confirm token is valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.OK)

        # delete the project, which should invalidate the token
        self.delete(
            '/projects/%(project_id)s' % {'project_id': self.projectA['id']})

        # user should no longer have access to the project
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.NOT_FOUND)
        self.v3_create_token(
            self.build_authentication_request(
                user_id=self.user3['id'],
                password=self.user3['password'],
                project_id=self.projectA['id']),
            expected_status=http_client.UNAUTHORIZED)

    def test_deleting_group_grant_revokes_tokens(self):
        """Test deleting a group grant revokes tokens.

        Test Plan:

        - Get a token for user1, scoped to ProjectA
        - Get a token for user2, scoped to ProjectA
        - Get a token for user3, scoped to ProjectA
        - Delete the grant group1 has on ProjectA
        - Check tokens for user1 & user2 are no longer valid,
          since user1 and user2 are members of group1
        - Check token for user3 is invalid too

        """
        auth_data = self.build_authentication_request(
            user_id=self.user1['id'],
            password=self.user1['password'],
            project_id=self.projectA['id'])
        token1 = self.get_requested_token(auth_data)
        auth_data = self.build_authentication_request(
            user_id=self.user2['id'],
            password=self.user2['password'],
            project_id=self.projectA['id'])
        token2 = self.get_requested_token(auth_data)
        auth_data = self.build_authentication_request(
            user_id=self.user3['id'],
            password=self.user3['password'],
            project_id=self.projectA['id'])
        token3 = self.get_requested_token(auth_data)
        # Confirm tokens are valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token1},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token2},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token3},
                  expected_status=http_client.OK)
        # Delete the group grant, which should invalidate the
        # tokens for user1 and user2
        grant_url = (
            '/projects/%(project_id)s/groups/%(group_id)s/'
            'roles/%(role_id)s' % {
                'project_id': self.projectA['id'],
                'group_id': self.group1['id'],
                'role_id': self.role1['id']})
        self.delete(grant_url)
        self.assignment_api.delete_grant(role_id=self.role1['id'],
                                         project_id=self.projectA['id'],
                                         user_id=self.user1['id'])
        self.assignment_api.delete_grant(role_id=self.role1['id'],
                                         project_id=self.projectA['id'],
                                         user_id=self.user2['id'])
        self.head('/auth/tokens', token=token1,
                  expected_status=http_client.UNAUTHORIZED)
        self.head('/auth/tokens', token=token2,
                  expected_status=http_client.UNAUTHORIZED)
        # But user3's token should be invalid too as revocation is done for
        # scope role & project
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token3},
                  expected_status=http_client.OK)

    def test_domain_group_role_assignment_maintains_token(self):
        """Test domain-group role assignment maintains existing token.

        Test Plan:

        - Get a token for user1, scoped to ProjectA
        - Create a grant for group1 on DomainB
        - Check token is still longer valid

        """
        auth_data = self.build_authentication_request(
            user_id=self.user1['id'],
            password=self.user1['password'],
            project_id=self.projectA['id'])
        token = self.get_requested_token(auth_data)
        # Confirm token is valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.OK)
        # Delete the grant, which should invalidate the token
        grant_url = (
            '/domains/%(domain_id)s/groups/%(group_id)s/'
            'roles/%(role_id)s' % {
                'domain_id': self.domainB['id'],
                'group_id': self.group1['id'],
                'role_id': self.role1['id']})
        self.put(grant_url)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.OK)

    def test_group_membership_changes_revokes_token(self):
        """Test add/removal to/from group revokes token.

        Test Plan:

        - Get a token for user1, scoped to ProjectA
        - Get a token for user2, scoped to ProjectA
        - Remove user1 from group1
        - Check token for user1 is no longer valid
        - Check token for user2 is still valid, even though
          user2 is also part of group1
        - Add user2 to group2
        - Check token for user2 is now no longer valid

        """
        auth_data = self.build_authentication_request(
            user_id=self.user1['id'],
            password=self.user1['password'],
            project_id=self.projectA['id'])
        token1 = self.get_requested_token(auth_data)
        auth_data = self.build_authentication_request(
            user_id=self.user2['id'],
            password=self.user2['password'],
            project_id=self.projectA['id'])
        token2 = self.get_requested_token(auth_data)
        # Confirm tokens are valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token1},
                  expected_status=http_client.OK)
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token2},
                  expected_status=http_client.OK)
        # Remove user1 from group1, which should invalidate
        # the token
        self.delete('/groups/%(group_id)s/users/%(user_id)s' % {
            'group_id': self.group1['id'],
            'user_id': self.user1['id']})
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token1},
                  expected_status=http_client.NOT_FOUND)
        # But user2's token should still be valid
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token2},
                  expected_status=http_client.OK)
        # Adding user2 to a group should not invalidate token
        self.put('/groups/%(group_id)s/users/%(user_id)s' % {
            'group_id': self.group2['id'],
            'user_id': self.user2['id']})
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token2},
                  expected_status=http_client.OK)

    def test_removing_role_assignment_does_not_affect_other_users(self):
        """Revoking a role from one user should not affect other users."""
        time = datetime.datetime.utcnow()
        with freezegun.freeze_time(time) as frozen_datetime:
            # This group grant is not needed for the test
            self.delete(
                '/projects/%(p_id)s/groups/%(g_id)s/roles/%(r_id)s' %
                {'p_id': self.projectA['id'],
                 'g_id': self.group1['id'],
                 'r_id': self.role1['id']})

            # NOTE(lbragstad): Here we advance the clock one second to pass
            # into the threshold of a new second because we just persisted a
            # revocation event for removing a role from a group on a project.
            # One thing to note about that revocation event is that it has no
            # context about the group, so even though user3 might not be in
            # group1, they could have their token revoked because the
            # revocation event is very general.
            frozen_datetime.tick(delta=datetime.timedelta(seconds=1))

            user1_token = self.get_requested_token(
                self.build_authentication_request(
                    user_id=self.user1['id'],
                    password=self.user1['password'],
                    project_id=self.projectA['id']))

            user3_token = self.get_requested_token(
                self.build_authentication_request(
                    user_id=self.user3['id'],
                    password=self.user3['password'],
                    project_id=self.projectA['id']))

            # delete relationships between user1 and projectA from setUp
            self.delete(
                '/projects/%(p_id)s/users/%(u_id)s/roles/%(r_id)s' % {
                    'p_id': self.projectA['id'],
                    'u_id': self.user1['id'],
                    'r_id': self.role1['id']})
            # authorization for the first user should now fail
            self.head('/auth/tokens',
                      headers={'X-Subject-Token': user1_token},
                      expected_status=http_client.NOT_FOUND)
            self.v3_create_token(
                self.build_authentication_request(
                    user_id=self.user1['id'],
                    password=self.user1['password'],
                    project_id=self.projectA['id']),
                expected_status=http_client.UNAUTHORIZED)

            # authorization for the second user should still succeed
            self.head('/auth/tokens',
                      headers={'X-Subject-Token': user3_token},
                      expected_status=http_client.OK)
            self.v3_create_token(
                self.build_authentication_request(
                    user_id=self.user3['id'],
                    password=self.user3['password'],
                    project_id=self.projectA['id']))

    def test_deleting_project_deletes_grants(self):
        # This is to make it a little bit more pretty with PEP8
        role_path = ('/projects/%(project_id)s/users/%(user_id)s/'
                     'roles/%(role_id)s')
        role_path = role_path % {'user_id': self.user['id'],
                                 'project_id': self.projectA['id'],
                                 'role_id': self.role['id']}

        # grant the user a role on the project
        self.put(role_path)

        # delete the project, which should remove the roles
        self.delete(
            '/projects/%(project_id)s' % {'project_id': self.projectA['id']})

        # Make sure that we get a 404 Not Found when heading that role.
        self.head(role_path, expected_status=http_client.NOT_FOUND)

    def get_v2_token(self, token=None, project_id=None):
        body = {'auth': {}, }

        if token:
            body['auth']['token'] = {
                'id': token
            }
        else:
            body['auth']['passwordCredentials'] = {
                'username': self.default_domain_user['name'],
                'password': self.default_domain_user['password'],
            }

        if project_id:
            body['auth']['tenantId'] = project_id

        r = self.admin_request(method='POST', path='/v2.0/tokens', body=body)
        return r.json_body['access']['token']['id']

    def test_revoke_v2_token_no_check(self):
        # Test that a V2 token can be revoked without validating it first.

        token = self.get_v2_token()

        self.delete('/auth/tokens',
                    headers={'X-Subject-Token': token})

        self.head('/auth/tokens',
                  headers={'X-Subject-Token': token},
                  expected_status=http_client.NOT_FOUND)

    def test_revoke_token_from_token(self):
        # Test that a scoped token can be requested from an unscoped token,
        # the scoped token can be revoked, and the unscoped token remains
        # valid.

        unscoped_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.user1['id'],
                password=self.user1['password']))

        # Get a project-scoped token from the unscoped token
        project_scoped_token = self.get_requested_token(
            self.build_authentication_request(
                token=unscoped_token,
                project_id=self.projectA['id']))

        # Get a domain-scoped token from the unscoped token
        domain_scoped_token = self.get_requested_token(
            self.build_authentication_request(
                token=unscoped_token,
                domain_id=self.domainA['id']))

        # revoke the project-scoped token.
        self.delete('/auth/tokens',
                    headers={'X-Subject-Token': project_scoped_token})

        # The project-scoped token is invalidated.
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': project_scoped_token},
                  expected_status=http_client.NOT_FOUND)

        # The unscoped token should still be valid.
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': unscoped_token},
                  expected_status=http_client.OK)

        # The domain-scoped token should still be valid.
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': domain_scoped_token},
                  expected_status=http_client.OK)

        # revoke the domain-scoped token.
        self.delete('/auth/tokens',
                    headers={'X-Subject-Token': domain_scoped_token})

        # The domain-scoped token is invalid.
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': domain_scoped_token},
                  expected_status=http_client.NOT_FOUND)

        # The unscoped token should still be valid.
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': unscoped_token},
                  expected_status=http_client.OK)

    def test_revoke_token_from_token_v2(self):
        # Test that a scoped token can be requested from an unscoped token,
        # the scoped token can be revoked, and the unscoped token remains
        # valid.

        unscoped_token = self.get_v2_token()

        # Get a project-scoped token from the unscoped token
        project_scoped_token = self.get_v2_token(
            token=unscoped_token, project_id=self.default_domain_project['id'])

        # revoke the project-scoped token.
        self.delete('/auth/tokens',
                    headers={'X-Subject-Token': project_scoped_token})

        # The project-scoped token is invalidated.
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': project_scoped_token},
                  expected_status=http_client.NOT_FOUND)

        # The unscoped token should still be valid.
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': unscoped_token},
                  expected_status=http_client.OK)


class TestTokenRevokeByAssignment(TestTokenRevokeById):

    def config_overrides(self):
        super(TestTokenRevokeById, self).config_overrides()
        self.config_fixture.config(
            group='token',
            provider='uuid',
            revoke_by_id=True)

    def test_removing_role_assignment_keeps_other_project_token_groups(self):
        """Test assignment isolation.

        Revoking a group role from one project should not invalidate all group
        users' tokens
        """
        self.assignment_api.create_grant(self.role1['id'],
                                         group_id=self.group1['id'],
                                         project_id=self.projectB['id'])

        project_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.user1['id'],
                password=self.user1['password'],
                project_id=self.projectB['id']))

        other_project_token = self.get_requested_token(
            self.build_authentication_request(
                user_id=self.user1['id'],
                password=self.user1['password'],
                project_id=self.projectA['id']))

        self.assignment_api.delete_grant(self.role1['id'],
                                         group_id=self.group1['id'],
                                         project_id=self.projectB['id'])

        # authorization for the projectA should still succeed
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': other_project_token},
                  expected_status=http_client.OK)
        # while token for the projectB should not
        self.head('/auth/tokens',
                  headers={'X-Subject-Token': project_token},
                  expected_status=http_client.NOT_FOUND)
        revoked_tokens = [
            t['id'] for t in self.token_provider_api.list_revoked_tokens()]
        # token is in token revocation list
        self.assertIn(project_token, revoked_tokens)


class TestTokenRevokeApi(TestTokenRevokeById):
    """Test token revocation on the v3 Identity API."""

    def config_overrides(self):
        super(TestTokenRevokeApi, self).config_overrides()
        self.config_fixture.config(
            group='token',
            provider='fernet',
            revoke_by_id=False)
        self.useFixture(
            ksfixtures.KeyRepository(
                self.config_fixture,
                'fernet_tokens',
                CONF.fernet_tokens.max_active_keys
            )
        )

    def assertValidDeletedProjectResponse(self, events_response, project_id):
        events = events_response['events']
        self.assertEqual(1, len(events))
        self.assertEqual(project_id, events[0]['project_id'])
        self.assertIsNotNone(events[0]['issued_before'])
        self.assertIsNotNone(events_response['links'])
        del (events_response['events'][0]['issued_before'])
        del (events_response['events'][0]['revoked_at'])
        del (events_response['links'])
        expected_response = {'events': [{'project_id': project_id}]}
        self.assertEqual(expected_response, events_response)

    def assertDomainAndProjectInList(self, events_response, domain_id):
        events = events_response['events']
        self.assertEqual(2, len(events))
        self.assertEqual(domain_id, events[0]['project_id'])
        self.assertEqual(domain_id, events[1]['domain_id'])
        self.assertIsNotNone(events[0]['issued_before'])
        self.assertIsNotNone(events[1]['issued_before'])
        self.assertIsNotNone(events_response['links'])
        del (events_response['events'][0]['issued_before'])
        del (events_response['events'][1]['issued_before'])
        del (events_response['events'][0]['revoked_at'])
        del (events_response['events'][1]['revoked_at'])
        del (events_response['links'])
        expected_response = {'events': [{'project_id': domain_id},
                                        {'domain_id': domain_id}]}
        self.assertEqual(expected_response, events_response)

    def assertValidRevokedTokenResponse(self, events_response, **kwargs):
        events = events_response['events']
        self.assertEqual(1, len(events))
        for k, v in kwargs.items():
            self.assertEqual(v, events[0].get(k))
        self.assertIsNotNone(events[0]['issued_before'])
        self.assertIsNotNone(events_response['links'])
        del (events_response['events'][0]['issued_before'])
        del (events_response['events'][0]['revoked_at'])
        del (events_response['links'])

        expected_response = {'events': [kwargs]}
        self.assertEqual(expected_response, events_response)

    def test_revoke_token(self):
        scoped_token = self.get_scoped_token()
        headers = {'X-Subject-Token': scoped_token}
        response = self.get('/auth/tokens', headers=headers).json_body['token']

        self.delete('/auth/tokens', headers=headers)
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.NOT_FOUND)
        events_response = self.get('/OS-REVOKE/events').json_body
        self.assertValidRevokedTokenResponse(events_response,
                                             audit_id=response['audit_ids'][0])

    def test_revoke_v2_token(self):
        token = self.get_v2_token()
        headers = {'X-Subject-Token': token}
        response = self.get('/auth/tokens',
                            headers=headers).json_body['token']
        self.delete('/auth/tokens', headers=headers)
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.NOT_FOUND)
        events_response = self.get('/OS-REVOKE/events').json_body

        self.assertValidRevokedTokenResponse(
            events_response,
            audit_id=response['audit_ids'][0])

    def test_revoke_by_id_false_returns_gone(self):
        self.get('/auth/tokens/OS-PKI/revoked',
                 expected_status=http_client.GONE)

    def test_list_delete_project_shows_in_event_list(self):
        self.role_data_fixtures()
        events = self.get('/OS-REVOKE/events').json_body['events']
        self.assertEqual([], events)
        self.delete(
            '/projects/%(project_id)s' % {'project_id': self.projectA['id']})
        events_response = self.get('/OS-REVOKE/events').json_body

        self.assertValidDeletedProjectResponse(events_response,
                                               self.projectA['id'])

    def test_disable_domain_shows_in_event_list(self):
        events = self.get('/OS-REVOKE/events').json_body['events']
        self.assertEqual([], events)
        disable_body = {'domain': {'enabled': False}}
        self.patch(
            '/domains/%(project_id)s' % {'project_id': self.domainA['id']},
            body=disable_body)

        events = self.get('/OS-REVOKE/events').json_body

        self.assertDomainAndProjectInList(events, self.domainA['id'])

    def assertEventDataInList(self, events, **kwargs):
        found = False
        for e in events:
            for key, value in kwargs.items():
                try:
                    if e[key] != value:
                        break
                except KeyError:
                    # Break the loop and present a nice error instead of
                    # KeyError
                    break
            else:
                # If the value of the event[key] matches the value of the kwarg
                # for each item in kwargs, the event was fully matched and
                # the assertTrue below should succeed.
                found = True
        self.assertTrue(found,
                        'event with correct values not in list, expected to '
                        'find event with key-value pairs. Expected: '
                        '"%(expected)s" Events: "%(events)s"' %
                        {'expected': ','.join(
                            ["'%s=%s'" % (k, v) for k, v in kwargs.items()]),
                         'events': events})

    def test_list_delete_token_shows_in_event_list(self):
        self.role_data_fixtures()
        events = self.get('/OS-REVOKE/events').json_body['events']
        self.assertEqual([], events)

        scoped_token = self.get_scoped_token()
        headers = {'X-Subject-Token': scoped_token}
        auth_req = self.build_authentication_request(token=scoped_token)
        response = self.v3_create_token(auth_req)
        token2 = response.json_body['token']
        headers2 = {'X-Subject-Token': response.headers['X-Subject-Token']}

        response = self.v3_create_token(auth_req)
        response.json_body['token']
        headers3 = {'X-Subject-Token': response.headers['X-Subject-Token']}

        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.OK)
        self.head('/auth/tokens', headers=headers2,
                  expected_status=http_client.OK)
        self.head('/auth/tokens', headers=headers3,
                  expected_status=http_client.OK)

        self.delete('/auth/tokens', headers=headers)
        # NOTE(ayoung): not deleting token3, as it should be deleted
        # by previous
        events_response = self.get('/OS-REVOKE/events').json_body
        events = events_response['events']
        self.assertEqual(1, len(events))
        self.assertEventDataInList(
            events,
            audit_id=token2['audit_ids'][1])
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.NOT_FOUND)
        self.head('/auth/tokens', headers=headers2,
                  expected_status=http_client.OK)
        self.head('/auth/tokens', headers=headers3,
                  expected_status=http_client.OK)

    def test_list_with_filter(self):

        self.role_data_fixtures()
        events = self.get('/OS-REVOKE/events').json_body['events']
        self.assertEqual(0, len(events))

        scoped_token = self.get_scoped_token()
        headers = {'X-Subject-Token': scoped_token}
        auth = self.build_authentication_request(token=scoped_token)
        headers2 = {'X-Subject-Token': self.get_requested_token(auth)}
        self.delete('/auth/tokens', headers=headers)
        self.delete('/auth/tokens', headers=headers2)

        events = self.get('/OS-REVOKE/events').json_body['events']

        self.assertEqual(2, len(events))
        future = utils.isotime(timeutils.utcnow() +
                               datetime.timedelta(seconds=1000))

        events = self.get('/OS-REVOKE/events?since=%s' % (future)
                          ).json_body['events']
        self.assertEqual(0, len(events))


class TestAuthExternalDisabled(test_v3.RestfulTestCase):
    def config_overrides(self):
        super(TestAuthExternalDisabled, self).config_overrides()
        self.config_fixture.config(
            group='auth',
            methods=['password', 'token'])

    def test_remote_user_disabled(self):
        api = auth.controllers.Auth()
        remote_user = '%s@%s' % (self.user['name'], self.domain['name'])
        request, auth_info, auth_context = self.build_external_auth_request(
            remote_user)
        self.assertRaises(exception.Unauthorized,
                          api.authenticate,
                          request,
                          auth_info,
                          auth_context)


class AuthExternalDomainBehavior(object):
    content_type = 'json'

    def test_remote_user_with_realm(self):
        api = auth.controllers.Auth()
        remote_user = self.user['name']
        remote_domain = self.domain['name']
        request, auth_info, auth_context = self.build_external_auth_request(
            remote_user, remote_domain=remote_domain, kerberos=self.kerberos)

        api.authenticate(request, auth_info, auth_context)
        self.assertEqual(self.user['id'], auth_context['user_id'])

        # Now test to make sure the user name can, itself, contain the
        # '@' character.
        user = {'name': 'myname@mydivision'}
        self.identity_api.update_user(self.user['id'], user)
        remote_user = user['name']
        request, auth_info, auth_context = self.build_external_auth_request(
            remote_user, remote_domain=remote_domain, kerberos=self.kerberos)

        api.authenticate(request, auth_info, auth_context)
        self.assertEqual(self.user['id'], auth_context['user_id'])

    def test_project_id_scoped_with_remote_user(self):
        self.config_fixture.config(group='token', bind=['kerberos'])
        auth_data = self.build_authentication_request(
            project_id=self.project['id'],
            kerberos=self.kerberos)
        remote_user = self.user['name']
        remote_domain = self.domain['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'REMOTE_DOMAIN': remote_domain,
                                             'AUTH_TYPE': 'Negotiate'})
        r = self.v3_create_token(auth_data)
        token = self.assertValidProjectScopedTokenResponse(r)
        self.assertEqual(self.user['name'], token['bind']['kerberos'])

    def test_unscoped_bind_with_remote_user(self):
        self.config_fixture.config(group='token', bind=['kerberos'])
        auth_data = self.build_authentication_request(kerberos=self.kerberos)
        remote_user = self.user['name']
        remote_domain = self.domain['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'REMOTE_DOMAIN': remote_domain,
                                             'AUTH_TYPE': 'Negotiate'})
        r = self.v3_create_token(auth_data)
        token = self.assertValidUnscopedTokenResponse(r)
        self.assertEqual(self.user['name'], token['bind']['kerberos'])


class TestAuthExternalDomainBehaviorWithUUID(AuthExternalDomainBehavior,
                                             test_v3.RestfulTestCase):
    def config_overrides(self):
        super(TestAuthExternalDomainBehaviorWithUUID, self).config_overrides()
        self.kerberos = False
        self.auth_plugin_config_override(external='Domain')
        self.config_fixture.config(group='token', provider='uuid')


# NOTE(lbragstad): The Fernet token provider doesn't support bind
# authentication so we don't inhereit TestAuthExternalDomain here to test it.


class TestAuthExternalDefaultDomain(object):
    content_type = 'json'

    def config_overrides(self):
        super(TestAuthExternalDefaultDomain, self).config_overrides()
        self.kerberos = False
        self.auth_plugin_config_override(
            external='keystone.auth.plugins.external.DefaultDomain')

    def test_remote_user_with_default_domain(self):
        api = auth.controllers.Auth()
        remote_user = self.default_domain_user['name']
        request, auth_info, auth_context = self.build_external_auth_request(
            remote_user, kerberos=self.kerberos)

        api.authenticate(request, auth_info, auth_context)
        self.assertEqual(self.default_domain_user['id'],
                         auth_context['user_id'])

        # Now test to make sure the user name can, itself, contain the
        # '@' character.
        user = {'name': 'myname@mydivision'}
        self.identity_api.update_user(self.default_domain_user['id'], user)
        remote_user = user['name']
        request, auth_info, auth_context = self.build_external_auth_request(
            remote_user, kerberos=self.kerberos)

        api.authenticate(request, auth_info, auth_context)
        self.assertEqual(self.default_domain_user['id'],
                         auth_context['user_id'])

    def test_project_id_scoped_with_remote_user(self):
        self.config_fixture.config(group='token', bind=['kerberos'])
        auth_data = self.build_authentication_request(
            project_id=self.default_domain_project['id'],
            kerberos=self.kerberos)
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})
        r = self.v3_create_token(auth_data)
        token = self.assertValidProjectScopedTokenResponse(r)
        self.assertEqual(self.default_domain_user['name'],
                         token['bind']['kerberos'])

    def test_unscoped_bind_with_remote_user(self):
        self.config_fixture.config(group='token', bind=['kerberos'])
        auth_data = self.build_authentication_request(kerberos=self.kerberos)
        remote_user = self.default_domain_user['name']
        self.admin_app.extra_environ.update({'REMOTE_USER': remote_user,
                                             'AUTH_TYPE': 'Negotiate'})
        r = self.v3_create_token(auth_data)
        token = self.assertValidUnscopedTokenResponse(r)
        self.assertEqual(self.default_domain_user['name'],
                         token['bind']['kerberos'])


class UUIDAuthExternalDefaultDomain(TestAuthExternalDefaultDomain,
                                    test_v3.RestfulTestCase):

    def config_overrides(self):
        super(UUIDAuthExternalDefaultDomain, self).config_overrides()
        self.config_fixture.config(group='token', provider='uuid')


class UUIDAuthKerberos(AuthExternalDomainBehavior, test_v3.RestfulTestCase):

    def config_overrides(self):
        super(UUIDAuthKerberos, self).config_overrides()
        self.kerberos = True
        self.config_fixture.config(group='token', provider='uuid')
        self.auth_plugin_config_override(
            methods=['kerberos', 'password', 'token'])


# NOTE(lbragstad): The Fernet token provider doesn't support bind
# authentication so we don't inherit AuthExternalDomainBehavior here to test
# it.


class TestAuthJSONExternal(test_v3.RestfulTestCase):
    content_type = 'json'

    def auth_plugin_config_override(self, methods=None, **method_classes):
        self.config_fixture.config(group='auth', methods=[])

    def test_remote_user_no_method(self):
        api = auth.controllers.Auth()
        request, auth_info, auth_context = self.build_external_auth_request(
            self.default_domain_user['name'])
        self.assertRaises(exception.Unauthorized,
                          api.authenticate,
                          request,
                          auth_info,
                          auth_context)


class TestTrustOptional(test_v3.RestfulTestCase):
    def config_overrides(self):
        super(TestTrustOptional, self).config_overrides()
        self.config_fixture.config(group='trust', enabled=False)

    def test_trusts_returns_not_found(self):
        self.get('/OS-TRUST/trusts', body={'trust': {}},
                 expected_status=http_client.NOT_FOUND)
        self.post('/OS-TRUST/trusts', body={'trust': {}},
                  expected_status=http_client.NOT_FOUND)

    def test_auth_with_scope_in_trust_forbidden(self):
        auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            trust_id=uuid.uuid4().hex)
        self.v3_create_token(auth_data,
                             expected_status=http_client.FORBIDDEN)


class TrustAPIBehavior(test_v3.RestfulTestCase):
    """Redelegation valid and secure.

    Redelegation is a hierarchical structure of trusts between initial trustor
    and a group of users allowed to impersonate trustor and act in his name.
    Hierarchy is created in a process of trusting already trusted permissions
    and organized as an adjacency list using 'redelegated_trust_id' field.
    Redelegation is valid if each subsequent trust in a chain passes 'not more'
    permissions than being redelegated.

    Trust constraints are:
     * roles - set of roles trusted by trustor
     * expiration_time
     * allow_redelegation - a flag
     * redelegation_count - decreasing value restricting length of trust chain
     * remaining_uses - DISALLOWED when allow_redelegation == True

    Trust becomes invalid in case:
     * trust roles were revoked from trustor
     * one of the users in the delegation chain was disabled or deleted
     * expiration time passed
     * one of the parent trusts has become invalid
     * one of the parent trusts was deleted

    """

    def config_overrides(self):
        super(TrustAPIBehavior, self).config_overrides()
        self.config_fixture.config(
            group='trust',
            enabled=True,
            allow_redelegation=True,
            max_redelegation_count=10
        )

    def setUp(self):
        super(TrustAPIBehavior, self).setUp()
        # Create a trustee to delegate stuff to
        self.trustee_user = unit.create_user(self.identity_api,
                                             domain_id=self.domain_id)

        # trustor->trustee
        self.redelegated_trust_ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=1),
            role_ids=[self.role_id],
            allow_redelegation=True)

        # trustor->trustee (no redelegation)
        self.chained_trust_ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            role_ids=[self.role_id],
            allow_redelegation=True)

    def _get_trust_token(self, trust):
        trust_id = trust['id']
        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust_id)
        trust_token = self.get_requested_token(auth_data)
        return trust_token

    def test_depleted_redelegation_count_error(self):
        self.redelegated_trust_ref['redelegation_count'] = 0
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': self.redelegated_trust_ref})
        trust = self.assertValidTrustResponse(r)
        trust_token = self._get_trust_token(trust)

        # Attempt to create a redelegated trust.
        self.post('/OS-TRUST/trusts',
                  body={'trust': self.chained_trust_ref},
                  token=trust_token,
                  expected_status=http_client.FORBIDDEN)

    def test_modified_redelegation_count_error(self):
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': self.redelegated_trust_ref})
        trust = self.assertValidTrustResponse(r)
        trust_token = self._get_trust_token(trust)

        # Attempt to create a redelegated trust with incorrect
        # redelegation_count.
        correct = trust['redelegation_count'] - 1
        incorrect = correct - 1
        self.chained_trust_ref['redelegation_count'] = incorrect
        self.post('/OS-TRUST/trusts',
                  body={'trust': self.chained_trust_ref},
                  token=trust_token,
                  expected_status=http_client.FORBIDDEN)

    def test_max_redelegation_count_constraint(self):
        incorrect = CONF.trust.max_redelegation_count + 1
        self.redelegated_trust_ref['redelegation_count'] = incorrect
        self.post('/OS-TRUST/trusts',
                  body={'trust': self.redelegated_trust_ref},
                  expected_status=http_client.FORBIDDEN)

    def test_redelegation_expiry(self):
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': self.redelegated_trust_ref})
        trust = self.assertValidTrustResponse(r)
        trust_token = self._get_trust_token(trust)

        # Attempt to create a redelegated trust supposed to last longer
        # than the parent trust: let's give it 10 minutes (>1 minute).
        too_long_live_chained_trust_ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=10),
            role_ids=[self.role_id])
        self.post('/OS-TRUST/trusts',
                  body={'trust': too_long_live_chained_trust_ref},
                  token=trust_token,
                  expected_status=http_client.FORBIDDEN)

    def test_redelegation_remaining_uses(self):
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': self.redelegated_trust_ref})
        trust = self.assertValidTrustResponse(r)
        trust_token = self._get_trust_token(trust)

        # Attempt to create a redelegated trust with remaining_uses defined.
        # It must fail according to specification: remaining_uses must be
        # omitted for trust redelegation. Any number here.
        self.chained_trust_ref['remaining_uses'] = 5
        self.post('/OS-TRUST/trusts',
                  body={'trust': self.chained_trust_ref},
                  token=trust_token,
                  expected_status=http_client.BAD_REQUEST)

    def test_roles_subset(self):
        # Build second role
        role = unit.new_role_ref()
        self.role_api.create_role(role['id'], role)
        # assign a new role to the user
        self.assignment_api.create_grant(role_id=role['id'],
                                         user_id=self.user_id,
                                         project_id=self.project_id)

        # Create first trust with extended set of roles
        ref = self.redelegated_trust_ref
        ref['expires_at'] = datetime.datetime.utcnow().replace(
            year=2032).strftime(unit.TIME_FORMAT)
        ref['roles'].append({'id': role['id']})
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': ref})
        trust = self.assertValidTrustResponse(r)
        # Trust created with exact set of roles (checked by role id)
        role_id_set = set(r['id'] for r in ref['roles'])
        trust_role_id_set = set(r['id'] for r in trust['roles'])
        self.assertEqual(role_id_set, trust_role_id_set)

        trust_token = self._get_trust_token(trust)

        # Chain second trust with roles subset
        self.chained_trust_ref['expires_at'] = (
            datetime.datetime.utcnow().replace(year=2028).strftime(
                unit.TIME_FORMAT))
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': self.chained_trust_ref},
                      token=trust_token)
        trust2 = self.assertValidTrustResponse(r)
        # First trust contains roles superset
        # Second trust contains roles subset
        role_id_set1 = set(r['id'] for r in trust['roles'])
        role_id_set2 = set(r['id'] for r in trust2['roles'])
        self.assertThat(role_id_set1, matchers.GreaterThan(role_id_set2))

    def test_redelegate_with_role_by_name(self):
        # For role by name testing
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=1),
            role_names=[self.role['name']],
            allow_redelegation=True)
        ref['expires_at'] = datetime.datetime.utcnow().replace(
            year=2032).strftime(unit.TIME_FORMAT)
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': ref})
        trust = self.assertValidTrustResponse(r)
        # Ensure we can get a token with this trust
        trust_token = self._get_trust_token(trust)
        # Chain second trust with roles subset
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            role_names=[self.role['name']],
            allow_redelegation=True)
        ref['expires_at'] = datetime.datetime.utcnow().replace(
            year=2028).strftime(unit.TIME_FORMAT)
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': ref},
                      token=trust_token)
        trust = self.assertValidTrustResponse(r)
        # Ensure we can get a token with this trust
        self._get_trust_token(trust)

    def test_redelegate_new_role_fails(self):
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': self.redelegated_trust_ref})
        trust = self.assertValidTrustResponse(r)
        trust_token = self._get_trust_token(trust)

        # Build second trust with a role not in parent's roles
        role = unit.new_role_ref()
        self.role_api.create_role(role['id'], role)
        # assign a new role to the user
        self.assignment_api.create_grant(role_id=role['id'],
                                         user_id=self.user_id,
                                         project_id=self.project_id)

        # Try to chain a trust with the role not from parent trust
        self.chained_trust_ref['roles'] = [{'id': role['id']}]

        # Bypass policy enforcement
        with mock.patch.object(rules, 'enforce', return_value=True):
            self.post('/OS-TRUST/trusts',
                      body={'trust': self.chained_trust_ref},
                      token=trust_token,
                      expected_status=http_client.FORBIDDEN)

    def test_redelegation_terminator(self):
        self.redelegated_trust_ref['expires_at'] = (
            datetime.datetime.utcnow().replace(year=2032).strftime(
                unit.TIME_FORMAT))
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': self.redelegated_trust_ref})
        trust = self.assertValidTrustResponse(r)
        trust_token = self._get_trust_token(trust)

        # Build second trust - the terminator
        self.chained_trust_ref['expires_at'] = (
            datetime.datetime.utcnow().replace(year=2028).strftime(
                unit.TIME_FORMAT))
        ref = dict(self.chained_trust_ref,
                   redelegation_count=1,
                   allow_redelegation=False)

        r = self.post('/OS-TRUST/trusts',
                      body={'trust': ref},
                      token=trust_token)

        trust = self.assertValidTrustResponse(r)
        # Check that allow_redelegation == False caused redelegation_count
        # to be set to 0, while allow_redelegation is removed
        self.assertNotIn('allow_redelegation', trust)
        self.assertEqual(0, trust['redelegation_count'])
        trust_token = self._get_trust_token(trust)

        # Build third trust, same as second
        self.post('/OS-TRUST/trusts',
                  body={'trust': ref},
                  token=trust_token,
                  expected_status=http_client.FORBIDDEN)

    def test_redelegation_without_impersonation(self):
        # Update trust to not allow impersonation
        self.redelegated_trust_ref['impersonation'] = False

        # Create trust
        resp = self.post('/OS-TRUST/trusts',
                         body={'trust': self.redelegated_trust_ref},
                         expected_status=http_client.CREATED)
        trust = self.assertValidTrustResponse(resp)

        # Get trusted token without impersonation
        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        trust_token = self.get_requested_token(auth_data)

        # Create second user for redelegation
        trustee_user_2 = unit.create_user(self.identity_api,
                                          domain_id=self.domain_id)

        # Trust for redelegation
        trust_ref_2 = unit.new_trust_ref(
            trustor_user_id=self.trustee_user['id'],
            trustee_user_id=trustee_user_2['id'],
            project_id=self.project_id,
            impersonation=False,
            expires=dict(minutes=1),
            role_ids=[self.role_id],
            allow_redelegation=False)

        # Creating a second trust should not be allowed since trustor does not
        # have the role to delegate thus returning 404 NOT FOUND.
        resp = self.post('/OS-TRUST/trusts',
                         body={'trust': trust_ref_2},
                         token=trust_token,
                         expected_status=http_client.NOT_FOUND)

    def test_create_unscoped_trust(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'])
        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        self.assertValidTrustResponse(r, ref)

    def test_create_trust_no_roles(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id)
        self.post('/OS-TRUST/trusts', body={'trust': ref},
                  expected_status=http_client.FORBIDDEN)

    def _initialize_test_consume_trust(self, count):
        # Make sure remaining_uses is decremented as we consume the trust
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            remaining_uses=count,
            role_ids=[self.role_id])
        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        # make sure the trust exists
        trust = self.assertValidTrustResponse(r, ref)
        r = self.get(
            '/OS-TRUST/trusts/%(trust_id)s' % {'trust_id': trust['id']})
        # get a token for the trustee
        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'])
        r = self.v3_create_token(auth_data)
        token = r.headers.get('X-Subject-Token')
        # get a trust token, consume one use
        auth_data = self.build_authentication_request(
            token=token,
            trust_id=trust['id'])
        r = self.v3_create_token(auth_data)
        return trust

    def test_consume_trust_once(self):
        trust = self._initialize_test_consume_trust(2)
        # check decremented value
        r = self.get(
            '/OS-TRUST/trusts/%(trust_id)s' % {'trust_id': trust['id']})
        trust = r.result.get('trust')
        self.assertIsNotNone(trust)
        self.assertEqual(1, trust['remaining_uses'])
        # FIXME(lbragstad): Assert the role that is returned is the right role.

    def test_create_one_time_use_trust(self):
        trust = self._initialize_test_consume_trust(1)
        # No more uses, the trust is made unavailable
        self.get(
            '/OS-TRUST/trusts/%(trust_id)s' % {'trust_id': trust['id']},
            expected_status=http_client.NOT_FOUND)
        # this time we can't get a trust token
        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_create_unlimited_use_trust(self):
        # by default trusts are unlimited in terms of tokens that can be
        # generated from them, this test creates such a trust explicitly
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            remaining_uses=None,
            role_ids=[self.role_id])
        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        trust = self.assertValidTrustResponse(r, ref)

        r = self.get(
            '/OS-TRUST/trusts/%(trust_id)s' % {'trust_id': trust['id']})
        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'])
        r = self.v3_create_token(auth_data)
        token = r.headers.get('X-Subject-Token')
        auth_data = self.build_authentication_request(
            token=token,
            trust_id=trust['id'])
        r = self.v3_create_token(auth_data)
        r = self.get(
            '/OS-TRUST/trusts/%(trust_id)s' % {'trust_id': trust['id']})
        trust = r.result.get('trust')
        self.assertIsNone(trust['remaining_uses'])

    def test_impersonation_token_cannot_create_new_trust(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=1),
            role_ids=[self.role_id])

        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        trust = self.assertValidTrustResponse(r)

        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])

        trust_token = self.get_requested_token(auth_data)

        # Build second trust
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=1),
            role_ids=[self.role_id])

        self.post('/OS-TRUST/trusts',
                  body={'trust': ref},
                  token=trust_token,
                  expected_status=http_client.FORBIDDEN)

    def test_trust_deleted_grant(self):
        # create a new role
        role = unit.new_role_ref()
        self.role_api.create_role(role['id'], role)

        grant_url = (
            '/projects/%(project_id)s/users/%(user_id)s/'
            'roles/%(role_id)s' % {
                'project_id': self.project_id,
                'user_id': self.user_id,
                'role_id': role['id']})

        # assign a new role
        self.put(grant_url)

        # create a trust that delegates the new role
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=False,
            expires=dict(minutes=1),
            role_ids=[role['id']])

        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        trust = self.assertValidTrustResponse(r)

        # delete the grant
        self.delete(grant_url)

        # attempt to get a trust token with the deleted grant
        # and ensure it's unauthorized
        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        r = self.v3_create_token(auth_data,
                                 expected_status=http_client.FORBIDDEN)

    def test_trust_chained(self):
        """Test that a trust token can't be used to execute another trust.

        To do this, we create an A->B->C hierarchy of trusts, then attempt to
        execute the trusts in series (C->B->A).

        """
        # create a sub-trustee user
        sub_trustee_user = unit.create_user(
            self.identity_api,
            domain_id=test_v3.DEFAULT_DOMAIN_ID)
        sub_trustee_user_id = sub_trustee_user['id']

        # create a new role
        role = unit.new_role_ref()
        self.role_api.create_role(role['id'], role)

        # assign the new role to trustee
        self.put(
            '/projects/%(project_id)s/users/%(user_id)s/roles/%(role_id)s' % {
                'project_id': self.project_id,
                'user_id': self.trustee_user['id'],
                'role_id': role['id']})

        # create a trust from trustor -> trustee
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=1),
            role_ids=[self.role_id])
        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        trust1 = self.assertValidTrustResponse(r)

        # authenticate as trustee so we can create a second trust
        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            project_id=self.project_id)
        token = self.get_requested_token(auth_data)

        # create a trust from trustee -> sub-trustee
        ref = unit.new_trust_ref(
            trustor_user_id=self.trustee_user['id'],
            trustee_user_id=sub_trustee_user_id,
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=1),
            role_ids=[role['id']])
        r = self.post('/OS-TRUST/trusts', token=token, body={'trust': ref})
        trust2 = self.assertValidTrustResponse(r)

        # authenticate as sub-trustee and get a trust token
        auth_data = self.build_authentication_request(
            user_id=sub_trustee_user['id'],
            password=sub_trustee_user['password'],
            trust_id=trust2['id'])
        trust_token = self.get_requested_token(auth_data)

        # attempt to get the second trust using a trust token
        auth_data = self.build_authentication_request(
            token=trust_token,
            trust_id=trust1['id'])
        r = self.v3_create_token(auth_data,
                                 expected_status=http_client.FORBIDDEN)

    def assertTrustTokensRevoked(self, trust_id):
        revocation_response = self.get('/OS-REVOKE/events')
        revocation_events = revocation_response.json_body['events']
        found = False
        for event in revocation_events:
            if event.get('OS-TRUST:trust_id') == trust_id:
                found = True
        self.assertTrue(found, 'event with trust_id %s not found in list' %
                        trust_id)

    def test_delete_trust_revokes_tokens(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=False,
            expires=dict(minutes=1),
            role_ids=[self.role_id])
        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        trust = self.assertValidTrustResponse(r)
        trust_id = trust['id']
        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust_id)
        r = self.v3_create_token(auth_data)
        self.assertValidProjectScopedTokenResponse(
            r, self.trustee_user)
        trust_token = r.headers['X-Subject-Token']
        self.delete('/OS-TRUST/trusts/%(trust_id)s' % {
            'trust_id': trust_id})
        headers = {'X-Subject-Token': trust_token}
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.NOT_FOUND)
        self.assertTrustTokensRevoked(trust_id)

    def disable_user(self, user):
        user['enabled'] = False
        self.identity_api.update_user(user['id'], user)

    def test_trust_get_token_fails_if_trustor_disabled(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=False,
            expires=dict(minutes=1),
            role_ids=[self.role_id])

        r = self.post('/OS-TRUST/trusts', body={'trust': ref})

        trust = self.assertValidTrustResponse(r, ref)

        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        self.v3_create_token(auth_data)

        self.disable_user(self.user)

        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.FORBIDDEN)

    def test_trust_get_token_fails_if_trustee_disabled(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=False,
            expires=dict(minutes=1),
            role_ids=[self.role_id])

        r = self.post('/OS-TRUST/trusts', body={'trust': ref})

        trust = self.assertValidTrustResponse(r, ref)

        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        self.v3_create_token(auth_data)

        self.disable_user(self.trustee_user)

        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_delete_trust(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=False,
            expires=dict(minutes=1),
            role_ids=[self.role_id])

        r = self.post('/OS-TRUST/trusts', body={'trust': ref})

        trust = self.assertValidTrustResponse(r, ref)

        self.delete('/OS-TRUST/trusts/%(trust_id)s' % {
            'trust_id': trust['id']})

        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_change_password_invalidates_trust_tokens(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=1),
            role_ids=[self.role_id])

        r = self.post('/OS-TRUST/trusts', body={'trust': ref})
        trust = self.assertValidTrustResponse(r)

        auth_data = self.build_authentication_request(
            user_id=self.trustee_user['id'],
            password=self.trustee_user['password'],
            trust_id=trust['id'])
        r = self.v3_create_token(auth_data)

        self.assertValidProjectScopedTokenResponse(r, self.user)
        trust_token = r.headers.get('X-Subject-Token')

        self.get('/OS-TRUST/trusts?trustor_user_id=%s' %
                 self.user_id, token=trust_token)

        self.assertValidUserResponse(
            self.patch('/users/%s' % self.trustee_user['id'],
                       body={'user': {'password': uuid.uuid4().hex}}))

        self.get('/OS-TRUST/trusts?trustor_user_id=%s' %
                 self.user_id, expected_status=http_client.UNAUTHORIZED,
                 token=trust_token)

    def test_trustee_can_do_role_ops(self):
        resp = self.post('/OS-TRUST/trusts',
                         body={'trust': self.redelegated_trust_ref})
        trust = self.assertValidTrustResponse(resp)
        trust_token = self._get_trust_token(trust)

        resp = self.get(
            '/OS-TRUST/trusts/%(trust_id)s/roles' % {
                'trust_id': trust['id']},
            token=trust_token)
        self.assertValidRoleListResponse(resp, self.role)

        self.head(
            '/OS-TRUST/trusts/%(trust_id)s/roles/%(role_id)s' % {
                'trust_id': trust['id'],
                'role_id': self.role['id']},
            token=trust_token,
            expected_status=http_client.OK)

        resp = self.get(
            '/OS-TRUST/trusts/%(trust_id)s/roles/%(role_id)s' % {
                'trust_id': trust['id'],
                'role_id': self.role['id']},
            token=trust_token)
        self.assertValidRoleResponse(resp, self.role)

    def test_do_not_consume_remaining_uses_when_get_token_fails(self):
        ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=self.trustee_user['id'],
            project_id=self.project_id,
            impersonation=False,
            expires=dict(minutes=1),
            role_ids=[self.role_id],
            remaining_uses=3)
        r = self.post('/OS-TRUST/trusts', body={'trust': ref})

        new_trust = r.result.get('trust')
        trust_id = new_trust.get('id')
        # Pass in another user's ID as the trustee, the result being a failed
        # token authenticate and the remaining_uses of the trust should not be
        # decremented.
        auth_data = self.build_authentication_request(
            user_id=self.default_domain_user['id'],
            password=self.default_domain_user['password'],
            trust_id=trust_id)
        self.v3_create_token(auth_data,
                             expected_status=http_client.FORBIDDEN)

        r = self.get('/OS-TRUST/trusts/%s' % trust_id)
        self.assertEqual(3, r.result.get('trust').get('remaining_uses'))


class TestTrustChain(test_v3.RestfulTestCase):

    def config_overrides(self):
        super(TestTrustChain, self).config_overrides()
        self.config_fixture.config(
            group='trust',
            enabled=True,
            allow_redelegation=True,
            max_redelegation_count=10
        )

    def setUp(self):
        super(TestTrustChain, self).setUp()
        """Create a trust chain using redelegation.

        A trust chain is a series of trusts that are redelegated. For example,
        self.user_list consists of userA, userB, and userC. The first trust in
        the trust chain is going to be established between self.user and userA,
        call it trustA. Then, userA is going to obtain a trust scoped token
        using trustA, and with that token create a trust between userA and
        userB called trustB. This pattern will continue with userB creating a
        trust with userC.
        So the trust chain should look something like:
            trustA -> trustB -> trustC
        Where:
            self.user is trusting userA with trustA
            userA is trusting userB with trustB
            userB is trusting userC with trustC

        """
        self.user_list = list()
        self.trust_chain = list()
        for _ in range(3):
            user = unit.create_user(self.identity_api,
                                    domain_id=self.domain_id)
            self.user_list.append(user)

        # trustor->trustee redelegation with impersonation
        trustee = self.user_list[0]
        trust_ref = unit.new_trust_ref(
            trustor_user_id=self.user_id,
            trustee_user_id=trustee['id'],
            project_id=self.project_id,
            impersonation=True,
            expires=dict(minutes=1),
            role_ids=[self.role_id],
            allow_redelegation=True,
            redelegation_count=3)

        # Create a trust between self.user and the first user in the list
        r = self.post('/OS-TRUST/trusts',
                      body={'trust': trust_ref})

        trust = self.assertValidTrustResponse(r)
        auth_data = self.build_authentication_request(
            user_id=trustee['id'],
            password=trustee['password'],
            trust_id=trust['id'])

        # Generate a trusted token for the first user
        trust_token = self.get_requested_token(auth_data)
        self.trust_chain.append(trust)

        # Loop through the user to create a chain of redelegated trust.
        for next_trustee in self.user_list[1:]:
            trust_ref = unit.new_trust_ref(
                trustor_user_id=self.user_id,
                trustee_user_id=next_trustee['id'],
                project_id=self.project_id,
                impersonation=True,
                role_ids=[self.role_id],
                allow_redelegation=True)
            r = self.post('/OS-TRUST/trusts',
                          body={'trust': trust_ref},
                          token=trust_token)
            trust = self.assertValidTrustResponse(r)
            auth_data = self.build_authentication_request(
                user_id=next_trustee['id'],
                password=next_trustee['password'],
                trust_id=trust['id'])
            trust_token = self.get_requested_token(auth_data)
            self.trust_chain.append(trust)

        trustee = self.user_list[-1]
        trust = self.trust_chain[-1]
        auth_data = self.build_authentication_request(
            user_id=trustee['id'],
            password=trustee['password'],
            trust_id=trust['id'])

        self.last_token = self.get_requested_token(auth_data)

    def assert_user_authenticate(self, user):
        auth_data = self.build_authentication_request(
            user_id=user['id'],
            password=user['password']
        )
        r = self.v3_create_token(auth_data)
        self.assertValidTokenResponse(r)

    def assert_trust_tokens_revoked(self, trust_id):
        trustee = self.user_list[0]
        auth_data = self.build_authentication_request(
            user_id=trustee['id'],
            password=trustee['password']
        )
        r = self.v3_create_token(auth_data)
        self.assertValidTokenResponse(r)

        revocation_response = self.get('/OS-REVOKE/events')
        revocation_events = revocation_response.json_body['events']
        found = False
        for event in revocation_events:
            if event.get('OS-TRUST:trust_id') == trust_id:
                found = True
        self.assertTrue(found, 'event with trust_id %s not found in list' %
                        trust_id)

    def test_delete_trust_cascade(self):
        self.assert_user_authenticate(self.user_list[0])
        self.delete('/OS-TRUST/trusts/%(trust_id)s' % {
            'trust_id': self.trust_chain[0]['id']})

        headers = {'X-Subject-Token': self.last_token}
        self.head('/auth/tokens', headers=headers,
                  expected_status=http_client.NOT_FOUND)
        self.assert_trust_tokens_revoked(self.trust_chain[0]['id'])

    def test_delete_broken_chain(self):
        self.assert_user_authenticate(self.user_list[0])
        self.delete('/OS-TRUST/trusts/%(trust_id)s' % {
            'trust_id': self.trust_chain[0]['id']})

        # Verify the two remaining trust have been deleted
        for i in range(len(self.user_list) - 1):
            auth_data = self.build_authentication_request(
                user_id=self.user_list[i]['id'],
                password=self.user_list[i]['password'])

            auth_token = self.get_requested_token(auth_data)

            # Assert chained trust have been deleted
            self.get('/OS-TRUST/trusts/%(trust_id)s' % {
                'trust_id': self.trust_chain[i + 1]['id']},
                token=auth_token,
                expected_status=http_client.NOT_FOUND)

    def test_trustor_roles_revoked(self):
        self.assert_user_authenticate(self.user_list[0])

        self.assignment_api.remove_role_from_user_and_project(
            self.user_id, self.project_id, self.role_id
        )

        # Verify that users are not allowed to authenticate with trust
        for i in range(len(self.user_list[1:])):
            trustee = self.user_list[i]
            auth_data = self.build_authentication_request(
                user_id=trustee['id'],
                password=trustee['password'])

            # Attempt to authenticate with trust
            token = self.get_requested_token(auth_data)
            auth_data = self.build_authentication_request(
                token=token,
                trust_id=self.trust_chain[i - 1]['id'])

            # Trustee has no delegated roles
            self.v3_create_token(auth_data,
                                 expected_status=http_client.FORBIDDEN)

    def test_intermediate_user_disabled(self):
        self.assert_user_authenticate(self.user_list[0])

        disabled = self.user_list[0]
        disabled['enabled'] = False
        self.identity_api.update_user(disabled['id'], disabled)

        # Bypass policy enforcement
        with mock.patch.object(rules, 'enforce', return_value=True):
            headers = {'X-Subject-Token': self.last_token}
            self.head('/auth/tokens', headers=headers,
                      expected_status=http_client.FORBIDDEN)

    def test_intermediate_user_deleted(self):
        self.assert_user_authenticate(self.user_list[0])

        self.identity_api.delete_user(self.user_list[0]['id'])

        # Bypass policy enforcement
        # Delete trustee will invalidate the trust.
        with mock.patch.object(rules, 'enforce', return_value=True):
            headers = {'X-Subject-Token': self.last_token}
            self.head('/auth/tokens', headers=headers,
                      expected_status=http_client.NOT_FOUND)


class TestAuthContext(unit.TestCase):
    def setUp(self):
        super(TestAuthContext, self).setUp()
        self.auth_context = auth.core.AuthContext()

    def test_pick_lowest_expires_at(self):
        expires_at_1 = utils.isotime(timeutils.utcnow())
        expires_at_2 = utils.isotime(timeutils.utcnow() +
                                     datetime.timedelta(seconds=10))
        # make sure auth_context picks the lowest value
        self.auth_context['expires_at'] = expires_at_1
        self.auth_context['expires_at'] = expires_at_2
        self.assertEqual(expires_at_1, self.auth_context['expires_at'])

    def test_identity_attribute_conflict(self):
        for identity_attr in auth.core.AuthContext.IDENTITY_ATTRIBUTES:
            self.auth_context[identity_attr] = uuid.uuid4().hex
            if identity_attr == 'expires_at':
                # 'expires_at' is a special case. Will test it in a separate
                # test case.
                continue
            self.assertRaises(exception.Unauthorized,
                              operator.setitem,
                              self.auth_context,
                              identity_attr,
                              uuid.uuid4().hex)

    def test_identity_attribute_conflict_with_none_value(self):
        for identity_attr in auth.core.AuthContext.IDENTITY_ATTRIBUTES:
            self.auth_context[identity_attr] = None

            if identity_attr == 'expires_at':
                # 'expires_at' is a special case and is tested above.
                self.auth_context['expires_at'] = uuid.uuid4().hex
                continue

            self.assertRaises(exception.Unauthorized,
                              operator.setitem,
                              self.auth_context,
                              identity_attr,
                              uuid.uuid4().hex)

    def test_non_identity_attribute_conflict_override(self):
        # for attributes Keystone doesn't know about, make sure they can be
        # freely manipulated
        attr_name = uuid.uuid4().hex
        attr_val_1 = uuid.uuid4().hex
        attr_val_2 = uuid.uuid4().hex
        self.auth_context[attr_name] = attr_val_1
        self.auth_context[attr_name] = attr_val_2
        self.assertEqual(attr_val_2, self.auth_context[attr_name])


class TestAuthSpecificData(test_v3.RestfulTestCase):

    def test_get_catalog_project_scoped_token(self):
        """Call ``GET /auth/catalog`` with a project-scoped token."""
        r = self.get('/auth/catalog')
        self.assertValidCatalogResponse(r)

    def test_get_catalog_domain_scoped_token(self):
        """Call ``GET /auth/catalog`` with a domain-scoped token."""
        # grant a domain role to a user
        self.put(path='/domains/%s/users/%s/roles/%s' % (
            self.domain['id'], self.user['id'], self.role['id']))

        self.get(
            '/auth/catalog',
            auth=self.build_authentication_request(
                user_id=self.user['id'],
                password=self.user['password'],
                domain_id=self.domain['id']),
            expected_status=http_client.FORBIDDEN)

    def test_get_catalog_unscoped_token(self):
        """Call ``GET /auth/catalog`` with an unscoped token."""
        self.get(
            '/auth/catalog',
            auth=self.build_authentication_request(
                user_id=self.default_domain_user['id'],
                password=self.default_domain_user['password']),
            expected_status=http_client.FORBIDDEN)

    def test_get_catalog_no_token(self):
        """Call ``GET /auth/catalog`` without a token."""
        self.get(
            '/auth/catalog',
            noauth=True,
            expected_status=http_client.UNAUTHORIZED)

    def test_get_projects_project_scoped_token(self):
        r = self.get('/auth/projects')
        self.assertThat(r.json['projects'], matchers.HasLength(1))
        self.assertValidProjectListResponse(r)

    def test_get_domains_project_scoped_token(self):
        self.put(path='/domains/%s/users/%s/roles/%s' % (
            self.domain['id'], self.user['id'], self.role['id']))

        r = self.get('/auth/domains')
        self.assertThat(r.json['domains'], matchers.HasLength(1))
        self.assertValidDomainListResponse(r)

    def test_get_projects_matches_federated_get_projects(self):
        # create at least one addition project to make sure it doesn't end up
        # in the response, since the user doesn't have any authorization on it
        ref = unit.new_project_ref(domain_id=CONF.identity.default_domain_id)
        r = self.post('/projects', body={'project': ref})
        unauthorized_project_id = r.json['project']['id']

        r = self.get('/auth/projects', expected_status=http_client.OK)
        self.assertThat(r.json['projects'], matchers.HasLength(1))
        for project in r.json['projects']:
            self.assertNotEqual(unauthorized_project_id, project['id'])

        expected_project_id = r.json['projects'][0]['id']

        # call GET /v3/OS-FEDERATION/projects
        r = self.get('/OS-FEDERATION/projects', expected_status=http_client.OK)

        # make sure the response is the same
        self.assertThat(r.json['projects'], matchers.HasLength(1))
        for project in r.json['projects']:
            self.assertEqual(expected_project_id, project['id'])

    def test_get_domains_matches_federated_get_domains(self):
        # create at least one addition domain to make sure it doesn't end up
        # in the response, since the user doesn't have any authorization on it
        ref = unit.new_domain_ref()
        r = self.post('/domains', body={'domain': ref})
        unauthorized_domain_id = r.json['domain']['id']

        ref = unit.new_domain_ref()
        r = self.post('/domains', body={'domain': ref})
        authorized_domain_id = r.json['domain']['id']

        path = '/domains/%(domain_id)s/users/%(user_id)s/roles/%(role_id)s' % {
            'domain_id': authorized_domain_id,
            'user_id': self.user_id,
            'role_id': self.role_id
        }
        self.put(path, expected_status=http_client.NO_CONTENT)

        r = self.get('/auth/domains', expected_status=http_client.OK)
        self.assertThat(r.json['domains'], matchers.HasLength(1))
        self.assertEqual(authorized_domain_id, r.json['domains'][0]['id'])
        self.assertNotEqual(unauthorized_domain_id, r.json['domains'][0]['id'])

        # call GET /v3/OS-FEDERATION/domains
        r = self.get('/OS-FEDERATION/domains', expected_status=http_client.OK)

        # make sure the response is the same
        self.assertThat(r.json['domains'], matchers.HasLength(1))
        self.assertEqual(authorized_domain_id, r.json['domains'][0]['id'])
        self.assertNotEqual(unauthorized_domain_id, r.json['domains'][0]['id'])


class TestTrustAuthFernetTokenProvider(TrustAPIBehavior, TestTrustChain):
    def config_overrides(self):
        super(TestTrustAuthFernetTokenProvider, self).config_overrides()
        self.config_fixture.config(group='token',
                                   provider='fernet',
                                   revoke_by_id=False)
        self.config_fixture.config(group='trust',
                                   enabled=True)
        self.useFixture(
            ksfixtures.KeyRepository(
                self.config_fixture,
                'fernet_tokens',
                CONF.fernet_tokens.max_active_keys
            )
        )


class TestAuthTOTP(test_v3.RestfulTestCase):

    def setUp(self):
        super(TestAuthTOTP, self).setUp()
        self.useFixture(
            ksfixtures.KeyRepository(
                self.config_fixture,
                'credential',
                credential_fernet.MAX_ACTIVE_KEYS
            )
        )

        ref = unit.new_totp_credential(
            user_id=self.default_domain_user['id'],
            project_id=self.default_domain_project['id'])

        self.secret = ref['blob']

        r = self.post('/credentials', body={'credential': ref})
        self.assertValidCredentialResponse(r, ref)

        self.addCleanup(self.cleanup)

    def auth_plugin_config_override(self):
        methods = ['totp', 'token', 'password']
        super(TestAuthTOTP, self).auth_plugin_config_override(methods)

    def _make_credentials(self, cred_type, count=1, user_id=None,
                          project_id=None, blob=None):
        user_id = user_id or self.default_domain_user['id']
        project_id = project_id or self.default_domain_project['id']

        creds = []
        for __ in range(count):
            if cred_type == 'totp':
                ref = unit.new_totp_credential(
                    user_id=user_id, project_id=project_id, blob=blob)
            else:
                ref = unit.new_credential_ref(
                    user_id=user_id, project_id=project_id)
            resp = self.post('/credentials', body={'credential': ref})
            creds.append(resp.json['credential'])
        return creds

    def _make_auth_data_by_id(self, passcode, user_id=None):
        return self.build_authentication_request(
            user_id=user_id or self.default_domain_user['id'],
            passcode=passcode,
            project_id=self.project['id'])

    def _make_auth_data_by_name(self, passcode, username, user_domain_id):
        return self.build_authentication_request(
            username=username,
            user_domain_id=user_domain_id,
            passcode=passcode,
            project_id=self.project['id'])

    def cleanup(self):
        totp_creds = self.credential_api.list_credentials_for_user(
            self.default_domain_user['id'], type='totp')

        other_creds = self.credential_api.list_credentials_for_user(
            self.default_domain_user['id'], type='other')

        for cred in itertools.chain(other_creds, totp_creds):
            self.delete('/credentials/%s' % cred['id'],
                        expected_status=http_client.NO_CONTENT)

    def test_with_a_valid_passcode(self):
        creds = self._make_credentials('totp')
        secret = creds[-1]['blob']

        # Stop the clock otherwise there is a chance of auth failure due to
        # getting a different TOTP between the call here and the call in the
        # auth plugin.
        self.useFixture(fixture.TimeFixture())

        auth_data = self._make_auth_data_by_id(
            totp._generate_totp_passcode(secret))

        self.v3_create_token(auth_data, expected_status=http_client.CREATED)

    def test_with_an_invalid_passcode_and_user_credentials(self):
        self._make_credentials('totp')
        auth_data = self._make_auth_data_by_id('000000')
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_with_an_invalid_passcode_with_no_user_credentials(self):
        auth_data = self._make_auth_data_by_id('000000')
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_with_a_corrupt_totp_credential(self):
        self._make_credentials('totp', count=1, blob='0')
        auth_data = self._make_auth_data_by_id('000000')
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_with_multiple_credentials(self):
        self._make_credentials('other', 3)
        creds = self._make_credentials('totp', count=3)
        secret = creds[-1]['blob']

        # Stop the clock otherwise there is a chance of auth failure due to
        # getting a different TOTP between the call here and the call in the
        # auth plugin.
        self.useFixture(fixture.TimeFixture())

        auth_data = self._make_auth_data_by_id(
            totp._generate_totp_passcode(secret))
        self.v3_create_token(auth_data, expected_status=http_client.CREATED)

    def test_with_multiple_users(self):
        # make some credentials for the existing user
        self._make_credentials('totp', count=3)

        # create a new user and their credentials
        user = unit.create_user(self.identity_api, domain_id=self.domain_id)
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=user['id'],
                                         project_id=self.project['id'])
        creds = self._make_credentials('totp', count=1, user_id=user['id'])
        secret = creds[-1]['blob']

        # Stop the clock otherwise there is a chance of auth failure due to
        # getting a different TOTP between the call here and the call in the
        # auth plugin.
        self.useFixture(fixture.TimeFixture())

        auth_data = self._make_auth_data_by_id(
            totp._generate_totp_passcode(secret), user_id=user['id'])
        self.v3_create_token(auth_data, expected_status=http_client.CREATED)

    def test_with_multiple_users_and_invalid_credentials(self):
        """Prevent logging in with someone else's credentials.

        It's very easy to forget to limit the credentials query by user.
        Let's just test it for a sanity check.
        """
        # make some credentials for the existing user
        self._make_credentials('totp', count=3)

        # create a new user and their credentials
        new_user = unit.create_user(self.identity_api,
                                    domain_id=self.domain_id)
        self.assignment_api.create_grant(self.role['id'],
                                         user_id=new_user['id'],
                                         project_id=self.project['id'])
        user2_creds = self._make_credentials(
            'totp', count=1, user_id=new_user['id'])

        user_id = self.default_domain_user['id']  # user1
        secret = user2_creds[-1]['blob']

        auth_data = self._make_auth_data_by_id(
            totp._generate_totp_passcode(secret), user_id=user_id)
        self.v3_create_token(auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_with_username_and_domain_id(self):
        creds = self._make_credentials('totp')
        secret = creds[-1]['blob']

        # Stop the clock otherwise there is a chance of auth failure due to
        # getting a different TOTP between the call here and the call in the
        # auth plugin.
        self.useFixture(fixture.TimeFixture())

        auth_data = self._make_auth_data_by_name(
            totp._generate_totp_passcode(secret),
            username=self.default_domain_user['name'],
            user_domain_id=self.default_domain_user['domain_id'])

        self.v3_create_token(auth_data, expected_status=http_client.CREATED)

    def test_generated_passcode_is_correct_format(self):
        secret = self._make_credentials('totp')[-1]['blob']
        passcode = totp._generate_totp_passcode(secret)
        reg = re.compile(r'^-?[0-9]+$')
        self.assertTrue(reg.match(passcode))


class TestFetchRevocationList(object):
    """Test fetch token revocation list on the v3 Identity API."""

    def config_overrides(self):
        super(TestFetchRevocationList, self).config_overrides()
        self.config_fixture.config(group='token', revoke_by_id=True)

    def test_ids_no_tokens(self):
        # When there's no revoked tokens the response is an empty list, and
        # the response is signed.
        res = self.get('/auth/tokens/OS-PKI/revoked')
        signed = res.json['signed']
        clear = cms.cms_verify(signed, CONF.signing.certfile,
                               CONF.signing.ca_certs)
        payload = json.loads(clear)
        self.assertEqual({'revoked': []}, payload)

    def test_ids_token(self):
        # When there's a revoked token, it's in the response, and the response
        # is signed.
        token_res = self.v3_create_token(
            self.build_authentication_request(
                user_id=self.user['id'],
                password=self.user['password'],
                project_id=self.project['id']))

        token_id = token_res.headers.get('X-Subject-Token')
        token_data = token_res.json['token']

        self.delete('/auth/tokens', headers={'X-Subject-Token': token_id})

        res = self.get('/auth/tokens/OS-PKI/revoked')
        signed = res.json['signed']
        clear = cms.cms_verify(signed, CONF.signing.certfile,
                               CONF.signing.ca_certs)
        payload = json.loads(clear)

        def truncate(ts_str):
            return ts_str[:19] + 'Z'  # 2016-01-21T15:53:52 == 19 chars.

        exp_token_revoke_data = {
            'id': token_id,
            'audit_id': token_data['audit_ids'][0],
            'expires': truncate(token_data['expires_at']),
        }

        self.assertEqual({'revoked': [exp_token_revoke_data]}, payload)

    def test_audit_id_only_no_tokens(self):
        # When there's no revoked tokens and ?audit_id_only is used, the
        # response is an empty list and is not signed.
        res = self.get('/auth/tokens/OS-PKI/revoked?audit_id_only')
        self.assertEqual({'revoked': []}, res.json)

    def test_audit_id_only_token(self):
        # When there's a revoked token and ?audit_id_only is used, the
        # response contains the audit_id of the token and is not signed.
        token_res = self.v3_create_token(
            self.build_authentication_request(
                user_id=self.user['id'],
                password=self.user['password'],
                project_id=self.project['id']))

        token_id = token_res.headers.get('X-Subject-Token')
        token_data = token_res.json['token']

        self.delete('/auth/tokens', headers={'X-Subject-Token': token_id})

        res = self.get('/auth/tokens/OS-PKI/revoked?audit_id_only')

        def truncate(ts_str):
            return ts_str[:19] + 'Z'  # 2016-01-21T15:53:52 == 19 chars.

        exp_token_revoke_data = {
            'audit_id': token_data['audit_ids'][0],
            'expires': truncate(token_data['expires_at']),
        }

        self.assertEqual({'revoked': [exp_token_revoke_data]}, res.json)


class UUIDFetchRevocationList(TestFetchRevocationList,
                              test_v3.RestfulTestCase):

    def config_overrides(self):
        super(UUIDFetchRevocationList, self).config_overrides()
        self.config_fixture.config(group='token', provider='uuid')


# NOTE(lbragstad): The Fernet token provider doesn't use Revocation lists so
# don't inherit TestFetchRevocationList here to test it.
