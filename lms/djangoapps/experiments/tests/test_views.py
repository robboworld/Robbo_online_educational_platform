"""
Tests for experimentation views
"""


import unittest

import six.moves.urllib.error
import six.moves.urllib.parse
import six.moves.urllib.request
from datetime import timedelta
from django.conf import settings
from django.core.handlers.wsgi import WSGIRequest
from django.utils.timezone import now
from django.test.utils import override_settings
from django.urls import reverse
from lms.djangoapps.course_blocks.transformers.tests.helpers import ModuleStoreTestCase
from mock import patch
from rest_framework.test import APITestCase

from lms.djangoapps.experiments.factories import ExperimentDataFactory, ExperimentKeyValueFactory
from lms.djangoapps.experiments.models import ExperimentData, ExperimentKeyValue
from lms.djangoapps.experiments.serializers import ExperimentDataSerializer
from common.djangoapps.student.tests.factories import UserFactory

from xmodule.modulestore.tests.factories import CourseFactory

CROSS_DOMAIN_REFERER = 'https://ecommerce.edx.org'


class ExperimentDataViewSetTests(APITestCase, ModuleStoreTestCase):

    def assert_data_created_for_user(self, user, method='post', status=201):
        url = reverse('api_experiments:v0:data-list')
        data = {
            'experiment_id': 1,
            'key': 'foo',
            'value': 'bar',
        }
        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)
        response = getattr(self.client, method)(url, data)
        self.assertEqual(response.status_code, status)

        # This will raise an exception if no data exists
        ExperimentData.objects.get(user=user)

        data['user'] = user.username
        self.assertDictContainsSubset(data, response.data)

    def test_list_permissions(self):
        """ Users should only be able to list their own data. """
        url = reverse('api_experiments:v0:data-list')
        user = UserFactory()

        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)

        ExperimentDataFactory()
        datum = ExperimentDataFactory(user=user)
        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'], ExperimentDataSerializer([datum], many=True).data)

    def test_list_filtering(self):
        """ Users should be able to filter by the experiment_id and key fields. """
        url = reverse('api_experiments:v0:data-list')
        user = UserFactory()
        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)

        experiment_id = 1
        ExperimentDataFactory()
        ExperimentDataFactory(user=user)
        data = ExperimentDataFactory.create_batch(3, user=user, experiment_id=experiment_id)

        qs = six.moves.urllib.parse.urlencode({'experiment_id': experiment_id})
        response = self.client.get('{url}?{qs}'.format(url=url, qs=qs))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'], ExperimentDataSerializer(data, many=True).data)

        datum = data[0]
        qs = six.moves.urllib.parse.urlencode({'key': datum.key})
        response = self.client.get('{url}?{qs}'.format(url=url, qs=qs))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'], ExperimentDataSerializer([datum], many=True).data)

        qs = six.moves.urllib.parse.urlencode({'experiment_id': experiment_id, 'key': datum.key})
        response = self.client.get('{url}?{qs}'.format(url=url, qs=qs))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'], ExperimentDataSerializer([datum], many=True).data)

    def test_read_permissions(self):
        """ Users should only be allowed to read their own data. """
        user = UserFactory()
        datum = ExperimentDataFactory(user=user)
        url = reverse('api_experiments:v0:data-detail', kwargs={'pk': datum.id})

        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)

        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        other_user = UserFactory()
        self.client.login(username=other_user.username, password=UserFactory._DEFAULT_PASSWORD)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_create_permissions(self):
        """ Users should only be allowed to create data for themselves. """
        url = reverse('api_experiments:v0:data-list')

        # Authentication is required
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 401)

        user = UserFactory()
        data = {
            'experiment_id': 1,
            'key': 'foo',
            'value': 'bar',
        }
        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)

        # Users can create data for themselves
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 201)
        ExperimentData.objects.get(user=user)

        # A non-staff user cannot create data for another user
        other_user = UserFactory()
        data['user'] = other_user.username
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ExperimentData.objects.filter(user=other_user).exists())

        # A staff user can create data for other users
        user.is_staff = True
        user.save()
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 201)
        ExperimentData.objects.get(user=other_user)

    def test_put_as_create(self):
        """ Users should be able to use PUT to create new data. """
        user = UserFactory()
        self.assert_data_created_for_user(user, 'put')

        # Subsequent requests should update the data
        self.assert_data_created_for_user(user, 'put', 200)

    def test_update_permissions(self):
        """ Users should only be allowed to update their own data. """
        user = UserFactory()
        other_user = UserFactory()
        datum = ExperimentDataFactory(user=user)
        url = reverse('api_experiments:v0:data-detail', kwargs={'pk': datum.id})
        data = {}

        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, 401)

        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)
        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, 200)

        self.client.login(username=other_user.username, password=UserFactory._DEFAULT_PASSWORD)
        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, 404)


def cross_domain_config(func):
    """Decorator for configuring a cross-domain request. """
    feature_flag_decorator = patch.dict(settings.FEATURES, {
        'ENABLE_CORS_HEADERS': True,
        'ENABLE_CROSS_DOMAIN_CSRF_COOKIE': True
    })
    settings_decorator = override_settings(
        CORS_ORIGIN_WHITELIST=['ecommerce.edx.org'],
        CSRF_COOKIE_NAME="prod-edx-csrftoken",
        CROSS_DOMAIN_CSRF_COOKIE_NAME="prod-edx-csrftoken",
        CROSS_DOMAIN_CSRF_COOKIE_DOMAIN=".edx.org"
    )
    is_secure_decorator = patch.object(WSGIRequest, 'is_secure', return_value=True)

    return feature_flag_decorator(
        settings_decorator(
            is_secure_decorator(func)
        )
    )


@unittest.skipUnless(settings.ROOT_URLCONF == 'lms.urls', 'Test only valid in lms')
class ExperimentCrossDomainTests(APITestCase):
    """Tests for handling cross-domain requests"""

    def setUp(self):
        super(ExperimentCrossDomainTests, self).setUp()
        self.client = self.client_class(enforce_csrf_checks=True)

    @cross_domain_config
    def test_cross_domain_create(self, *args):  # pylint: disable=unused-argument
        user = UserFactory()
        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)  # pylint: disable=protected-access
        csrf_cookie = self._get_csrf_cookie()
        data = {
            'experiment_id': 1,
            'key': 'foo',
            'value': 'bar',
        }
        resp = self._cross_domain_post(csrf_cookie, data)

        # Expect that the request gets through successfully,
        # passing the CSRF checks (including the referer check).
        self.assertEqual(resp.status_code, 201)

    @cross_domain_config
    def test_cross_domain_invalid_csrf_header(self, *args):  # pylint: disable=unused-argument
        user = UserFactory()
        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)  # pylint: disable=protected-access
        self._get_csrf_cookie()
        data = {
            'experiment_id': 1,
            'key': 'foo',
            'value': 'bar',
        }
        resp = self._cross_domain_post('invalid_csrf_token', data)
        self.assertEqual(resp.status_code, 403)

    @cross_domain_config
    def test_cross_domain_not_in_whitelist(self, *args):  # pylint: disable=unused-argument
        user = UserFactory()
        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)  # pylint: disable=protected-access
        csrf_cookie = self._get_csrf_cookie()
        data = {
            'experiment_id': 1,
            'key': 'foo',
            'value': 'bar',
        }
        resp = self._cross_domain_post(csrf_cookie, data, referer='www.example.com')
        self.assertEqual(resp.status_code, 403)

    def _get_csrf_cookie(self):
        """Retrieve the cross-domain CSRF cookie. """
        url = reverse('courseenrollments')
        resp = self.client.get(url, HTTP_REFERER=CROSS_DOMAIN_REFERER)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, resp.cookies)
        return resp.cookies[settings.CSRF_COOKIE_NAME].value

    def _cross_domain_post(self, csrf_token, data, referer=CROSS_DOMAIN_REFERER):
        """Perform a cross-domain POST request. """
        url = reverse('api_experiments:v0:data-list')
        kwargs = {
            'HTTP_REFERER': referer,
            settings.CSRF_HEADER_NAME: csrf_token,
        }
        return self.client.post(
            url,
            data,
            **kwargs
        )


class ExperimentKeyValueViewSetTests(APITestCase):

    def test_permissions(self):
        """ Staff access is required for write operations. """
        url = reverse('api_experiments:v0:key_value-list')

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 401)

        instance = ExperimentKeyValueFactory()
        url = reverse('api_experiments:v0:key_value-detail', kwargs={'pk': instance.id})

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        user = UserFactory(is_staff=False)
        self.client.login(username=user.username, password=UserFactory._DEFAULT_PASSWORD)

        response = self.client.put(url, {})
        self.assertEqual(response.status_code, 403)

        response = self.client.patch(url, {})
        self.assertEqual(response.status_code, 403)

        response = self.client.delete(url)
        self.assertEqual(response.status_code, 403)


class ExperimentUserMetaDataViewTests(APITestCase, ModuleStoreTestCase):
    """ Internal user_metadata view/API for use in Optimizely experiments """

    def test_UserMetaDataView_get_success_same_user(self):
        """ Request succeeds when logged-in user makes request for self """
        lookup_user = UserFactory()
        lookup_course = CourseFactory.create(start=now() - timedelta(days=30))
        call_args = [lookup_user.username, lookup_course.id]
        self.client.login(username=lookup_user.username, password=UserFactory._DEFAULT_PASSWORD)

        response = self.client.get(reverse('api_experiments:user_metadata', args=call_args))
        self.assertEqual(response.status_code, 200)

    def test_UserMetaDataView_get_success_staff_user(self):
        """ Request succeeds when logged-in staff user makes request for different user """
        lookup_user = UserFactory()
        lookup_course = CourseFactory.create(start=now() - timedelta(days=30))
        call_args = [lookup_user.username, lookup_course.id]
        staff_user = UserFactory(is_staff=True)

        self.client.login(username=staff_user.username, password=UserFactory._DEFAULT_PASSWORD)

        response = self.client.get(reverse('api_experiments:user_metadata', args=call_args))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['course_id'])
        self.assertTrue(response.json()['user_id'])
        self.assertEqual(response.json()['username'], lookup_user.username)
        self.assertEqual(response.json()['email'], lookup_user.email)

    def test_UserMetaDataView_get_different_user(self):
        """ Request fails when not logged in for requested user or staff  """
        lookup_user = UserFactory()
        lookup_course = CourseFactory.create(start=now() - timedelta(days=30))
        call_args = [lookup_user.username, lookup_course.id]

        response = self.client.get(reverse('api_experiments:user_metadata', args=call_args))
        self.assertEqual(response.status_code, 401)

    def test_UserMetaDataView_get_missing_course(self):
        """ Request fails when not course not found  """
        lookup_user = UserFactory()
        lookup_course = CourseFactory.create(start=now() - timedelta(days=30))
        call_args = [lookup_user.username, lookup_course.id]
        self.client.login(username=lookup_user.username, password=UserFactory._DEFAULT_PASSWORD)
        bogus_course_name = str(lookup_course.id) + '_FOOBAR'

        call_args_with_bogus_course = [lookup_user.username, bogus_course_name]
        response = self.client.get(reverse('api_experiments:user_metadata', args=call_args_with_bogus_course))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['message'], 'Provided course is not found')
