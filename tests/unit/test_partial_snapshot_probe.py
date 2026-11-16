# -*- coding: utf-8 -*-

# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""The live partial-snapshot helper, without a cluster.

The ladder is the thing that talks to VergeOS. These tests pin the order
that ladder depends on: poll a fresh read after create(wait=True), call
find_vms, and only then read cloud_snapshot_vms. They also pin the filter
and the POST body, which are easy to "tidy" into something the platform
does not fill in.
"""

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'live'))
import partial_snapshot_probe as probe  # noqa: E402


class Snap(dict):
    """A snapshot row that dict() and .get both see."""


class Cloud:
    def __init__(self, created_status='normal', gets=None):
        self.created = None
        self.gets = []
        self.deleted = []
        self._created_status = created_status
        self._gets = list(gets or [])

    def create(self, **kwargs):
        self.created = kwargs
        return Snap({'$key': 9, 'status': self._created_status})

    def get(self, key):
        self.gets.append(key)
        if self._gets:
            status = self._gets.pop(0)
        else:
            status = 'normal'
        return Snap({'$key': key, 'status': status})

    def delete(self, key):
        self.deleted.append(key)


class Client:
    def __init__(self, cloud=None):
        self.cloud_snapshots = cloud or Cloud()
        self.calls = []
        self._members = [
            {'name': 'zz-partial-in', 'is_snapshot': False,
             'cloud_snapshot': 9},
        ]
        self._snapshots = []
        self.snapshot_profiles = None

    def _request(self, method, path, params=None, json_data=None):
        self.calls.append({
            'method': method, 'path': path,
            'params': params, 'json_data': json_data,
        })
        if path == 'cloud_snapshot_vms':
            return list(self._members)
        if path == 'cloud_snapshots':
            return list(self._snapshots)
        return None


def test_settled_treats_a_missing_status_as_not_done():
    assert probe.settled(None) is False
    assert probe.settled('') is False
    assert probe.settled('building') is False
    assert probe.settled('normal') is True


def test_create_refuses_to_snapshot_the_whole_cloud():
    client = Client()
    with pytest.raises(ValueError, match='whole cloud'):
        probe.create_partial(client, 'zz-partial-snap', '')
    assert client.cloud_snapshots.created is None
    assert client.calls == []


def test_create_without_the_parameter_does_not_call_create():
    class OldCloud:
        def create(self, name=None, wait=False, wait_timeout=300,
                   retention_seconds=None):
            raise AssertionError('create was called')

    client = Client()
    client.cloud_snapshots = OldCloud()
    with pytest.raises(ValueError, match='include_tags'):
        probe.create_partial(client, 'zz-partial-snap', 'zz-partial')


def test_create_polls_before_find_vms_and_uses_the_sdk_parameter():
    """wait=True's return value is not the status we trust (#202).

    The first fresh read is still building. find_vms runs only after a
    later read has left building, and the membership GET is after that
    (#201). The write is include_tags, not a column name.
    """
    cloud = Cloud(created_status='normal', gets=['building', 'normal'])
    client = Client(cloud)
    result = probe.create_partial(
        client, 'zz-partial-snap', 'zz-partial', timeout=30)

    assert cloud.created['include_tags'] == ['zz-partial']
    assert cloud.created['wait'] is True
    assert cloud.created['name'] == 'zz-partial-snap'
    assert 'partial_tags' not in cloud.created
    assert 'partial_tag_args' not in cloud.created
    assert cloud.gets == [9, 9]
    assert [call['path'] for call in client.calls] == [
        'cloud_snapshot_actions', 'cloud_snapshot_vms']
    assert client.calls[0]['json_data'] == {
        'action': 'find_vms', 'cloud_snapshot': 9}
    assert client.calls[1]['params']['filter'] == (
        'cloud_snapshot eq 9 and is_snapshot eq false')
    assert result == {
        'key': 9,
        'status': 'normal',
        'members': ['zz-partial-in'],
    }


def test_a_wait_timeout_still_polls_the_row_it_created(monkeypatch):
    class Slow(Exception):
        pass

    cloud = Cloud(gets=['building', 'normal'])

    def explode(**_kwargs):
        raise Slow('gave up')

    cloud.create = explode
    client = Client(cloud)
    client._snapshots = [{'$key': 9, 'name': 'zz-partial-snap'}]
    monkeypatch.setattr(probe, '_wait_timeout_type', lambda: Slow)

    result = probe.create_partial(client, 'zz-partial-snap', 'zz-partial')

    assert result['key'] == 9
    assert result['status'] == 'normal'
    assert client.calls[0]['path'] == 'cloud_snapshots'
    assert client.calls[1]['json_data']['action'] == 'find_vms'


def test_still_building_at_the_deadline_is_an_error():
    cloud = Cloud(gets=['building', 'building', 'building'])
    client = Client(cloud)
    ticks = iter([0, 0, 50])
    monkeypatch_time = probe.time.time
    probe.time.time = lambda: next(ticks)
    try:
        with pytest.raises(TimeoutError, match='still building'):
            probe.wait_until_settled(client, 9, timeout=5)
    finally:
        probe.time.time = monkeypatch_time
    assert client.calls == []


def test_delete_removes_only_the_named_snapshot():
    client = Client()
    client._snapshots = [
        {'$key': 9, 'name': 'zz-partial-snap'},
        {'$key': 10, 'name': 'nightly'},
    ]
    result = probe.delete_named(client, 'zz-partial-snap')
    assert result == {'deleted': [9]}
    assert client.cloud_snapshots.deleted == [9]


def test_delete_of_a_missing_name_is_empty():
    client = Client()
    assert probe.delete_named(client, 'zz-partial-snap') == {'deleted': []}


class Period(dict):
    def __init__(self, data, kind, partial, quiesce):
        super(Period, self).__init__(data)
        self.snapshot_type = kind
        self.partial_tag_keys = partial
        self.quiesce_tag_keys = quiesce


class Profiles:
    def __init__(self, periods):
        self._periods = periods
        self.listed_fields = None
        self._rows = [{'$key': 4, 'name': 'protect-zz-partial'}]

    def list(self):
        return list(self._rows)

    def periods(self, key):
        assert key == 4
        parent = self

        class Manager:
            def list(self, fields=None):
                parent.listed_fields = fields
                return list(parent._periods)

        return Manager()


def test_show_period_reads_the_sdk_properties_and_the_projection():
    period = Period(
        {'$key': 8, 'name': 'Daily', 'type': 'partial_include'},
        'partial_include', [5], [5])
    profiles = Profiles([period])
    client = Client()
    client.snapshot_profiles = profiles

    result = probe.show_period(client, 'protect-zz-partial', 'Daily')

    assert result == {
        'profile_key': 4,
        'period_key': 8,
        'snapshot_type': 'partial_include',
        'partial_tag_keys': [5],
        'quiesce_tag_keys': [5],
    }
    assert profiles.listed_fields == [
        '$key', 'name', 'type',
        'partial_tags[refs[tag]]',
        'quiesce_tags[refs[tag]]',
    ]


def test_show_period_refuses_an_sdk_that_cannot_read_the_scope():
    profiles = Profiles([{' $key': 8, 'name': 'Daily'}])
    # A plain dict has no snapshot_type. That is a released pyVergeOS.
    profiles._periods = [{'$key': 8, 'name': 'Daily'}]
    client = Client()
    client.snapshot_profiles = profiles

    with pytest.raises(ValueError, match='200'):
        probe.show_period(client, 'protect-zz-partial', 'Daily')


def test_show_period_refuses_two_profiles_of_the_same_name():
    profiles = Profiles([])
    profiles._rows = [
        {'$key': 4, 'name': 'protect-zz-partial'},
        {'$key': 5, 'name': 'protect-zz-partial'},
    ]
    client = Client()
    client.snapshot_profiles = profiles

    with pytest.raises(ValueError, match='found 2'):
        probe.show_period(client, 'protect-zz-partial', 'Daily')
