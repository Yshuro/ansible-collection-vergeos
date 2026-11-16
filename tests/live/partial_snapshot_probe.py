#!/usr/bin/env python
# -*- coding: utf-8 -*-

# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Live helper for a tag-scoped cloud snapshot.

Verbs, all of them JSON on stdout:

  show-period --profile NAME --period NAME
      Read one snapshot profile period through the pyVergeOS #200
      properties (snapshot_type, partial_tag_keys, quiesce_tag_keys).
      An SDK that does not have those fails here, by name.

  create --name NAME --include-tag TAG [--timeout SECONDS]
      cloud_snapshots.create(include_tags=[TAG], wait=True), then a fresh
      read until status is set and is not ``building``. wait=True can
      return while the row is still building (pyVergeOS #202), so the
      return value of create() is not the check. Then
      POST cloud_snapshot_actions {action: find_vms, cloud_snapshot: N}.
      Membership stays empty until that runs (pyVergeOS #201). The VM
      names come from
      GET cloud_snapshot_vms?filter=cloud_snapshot eq N and is_snapshot eq false.

  delete --name NAME
      Delete every cloud snapshot of that name. Missing is success.

Refuses to create without an include tag. That call would snapshot the
whole cloud. Auth is VERGEOS_* in the environment, same as protect_ctl.
"""

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import argparse
import inspect
import json
import os
import sys
import time


# Same projection pyVergeOS #200 puts on a period so partial_tag_keys has
# something to read. partial_tags / quiesce_tags alone are the row, not the tags.
PERIOD_FIELDS = [
    '$key',
    'name',
    'type',
    'partial_tags[refs[tag]]',
    'quiesce_tags[refs[tag]]',
]

MEMBER_FILTER = 'cloud_snapshot eq %d and is_snapshot eq false'
MEMBER_FIELDS = 'name,original_key,is_snapshot,cloud_snapshot'


def client():
    from pyvergeos import VergeClient
    kwargs = {
        'host': os.environ['VERGEOS_HOST'],
        'verify_ssl': os.environ.get('VERGEOS_INSECURE', '') not in (
            '1', 'true', 'True'),
    }
    if os.environ.get('VERGEOS_TOKEN'):
        kwargs['token'] = os.environ['VERGEOS_TOKEN']
    else:
        kwargs['username'] = os.environ['VERGEOS_USERNAME']
        kwargs['password'] = os.environ['VERGEOS_PASSWORD']
    return VergeClient(**kwargs)


def _as_rows(payload):
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [payload]
    return list(payload)


def settled(status):
    """A snapshot read is done when status is present and not building.

    A missing status is not done. The accessor on the SDK defaults that
    to ``normal``, which is how wait=True can return early (#202). Read
    the field.
    """
    return bool(status) and status != 'building'


def _status_of(snap):
    try:
        return dict(snap).get('status')
    except (TypeError, ValueError):
        return None


def _cloud_tags_supported(create):
    if create is None:
        return False
    try:
        signature = inspect.signature(create)
    except (TypeError, ValueError):
        return False
    if 'include_tags' in signature.parameters:
        return True
    return any(param.kind is inspect.Parameter.VAR_KEYWORD
               for param in signature.parameters.values())


def _snapshot_key(sdk, name):
    rows = _as_rows(sdk._request(
        'GET', 'cloud_snapshots', params={'fields': '$key,name'}))
    keys = [int(row['$key']) for row in rows if row.get('name') == name]
    if not keys:
        return None
    return keys[-1]


def wait_until_settled(sdk, key, timeout):
    """Poll a fresh GET until the snapshot leaves ``building``."""
    start = time.time()
    last = None
    while True:
        snap = sdk.cloud_snapshots.get(int(key))
        last = _status_of(snap)
        if settled(last):
            return snap, last
        if timeout > 0 and (time.time() - start) > timeout:
            raise TimeoutError(
                'cloud snapshot %s still building after %s seconds '
                '(last status: %r)' % (key, timeout, last))
        time.sleep(2)


def find_vms(sdk, key):
    """Fill cloud_snapshot_vms. The list stays empty until this runs (#201)."""
    sdk._request(
        'POST', 'cloud_snapshot_actions',
        json_data={'action': 'find_vms', 'cloud_snapshot': int(key)},
    )


def member_names(sdk, key):
    rows = _as_rows(sdk._request(
        'GET', 'cloud_snapshot_vms',
        params={
            'filter': MEMBER_FILTER % int(key),
            'fields': MEMBER_FIELDS,
        },
    ))
    return [str(row.get('name') or '') for row in rows]


def _wait_timeout_type():
    from pyvergeos.exceptions import VergeTimeoutError
    return VergeTimeoutError


def create_partial(sdk, name, include_tag, timeout=300):
    """One include-scoped cloud snapshot, settled, with VM names filled in."""
    include_tag = str(include_tag or '').strip()
    if not include_tag:
        raise ValueError(
            'refusing to create a cloud snapshot without an include tag. '
            'That would capture the whole cloud.')
    create = getattr(getattr(sdk, 'cloud_snapshots', None), 'create', None)
    if not _cloud_tags_supported(create):
        raise ValueError(
            'this pyVergeOS cloud_snapshots.create() does not accept '
            'include_tags. Tag-scoped cloud snapshots need the parameter '
            'added with pyVergeOS #129.')
    try:
        snap = sdk.cloud_snapshots.create(
            name=name,
            include_tags=[include_tag],
            retention_seconds=3600,
            wait=True,
            wait_timeout=timeout,
        )
        key = dict(snap).get('$key')
    except _wait_timeout_type():
        # create(wait=True) gave up, but the row may already exist (#202).
        key = _snapshot_key(sdk, name)
        if key is None:
            raise
    if key is None:
        raise ValueError('cloud snapshot create returned no $key')
    key = int(key)
    _snap, status = wait_until_settled(sdk, key, timeout)
    find_vms(sdk, key)
    return {
        'key': key,
        'status': status,
        'members': member_names(sdk, key),
    }


def delete_named(sdk, name):
    rows = _as_rows(sdk._request(
        'GET', 'cloud_snapshots', params={'fields': '$key,name'}))
    deleted = []
    for row in rows:
        if row.get('name') == name and row.get('$key') is not None:
            sdk.cloud_snapshots.delete(int(row['$key']))
            deleted.append(int(row['$key']))
    return {'deleted': deleted}


def show_period(sdk, profile_name, period_name):
    """One period's tag scope, via the properties #200 added."""
    profiles = [dict(row) for row in sdk.snapshot_profiles.list()]
    matches = [row for row in profiles if row.get('name') == profile_name]
    if len(matches) != 1:
        raise ValueError(
            'expected one snapshot profile named %r, found %d'
            % (profile_name, len(matches)))
    profile_key = int(matches[0]['$key'])
    periods = sdk.snapshot_profiles.periods(profile_key).list(
        fields=list(PERIOD_FIELDS))
    found = None
    for period in periods:
        if dict(period).get('name') == period_name:
            found = period
            break
    if found is None:
        raise ValueError(
            'no period %r on profile %r' % (period_name, profile_name))
    kind = getattr(found, 'snapshot_type', None)
    partial = getattr(found, 'partial_tag_keys', None)
    quiesce = getattr(found, 'quiesce_tag_keys', None)
    if (not isinstance(kind, str) or not isinstance(partial, list)
            or not isinstance(quiesce, list)):
        raise ValueError(
            'this pyVergeOS cannot read a period tag scope '
            '(snapshot_type, partial_tag_keys, quiesce_tag_keys). '
            'Those landed in PR #200, which is on dev and not in a '
            'release yet.')
    return {
        'profile_key': profile_key,
        'period_key': int(dict(found)['$key']),
        'snapshot_type': kind,
        'partial_tag_keys': [int(key) for key in partial],
        'quiesce_tag_keys': [int(key) for key in quiesce],
    }


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'verb', choices=['show-period', 'create', 'delete'])
    parser.add_argument('--profile')
    parser.add_argument('--period')
    parser.add_argument('--name')
    parser.add_argument('--include-tag')
    parser.add_argument('--timeout', type=int, default=300)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    sdk = client()
    try:
        if args.verb == 'show-period':
            if not args.profile or not args.period:
                parser.error('--profile and --period are required')
            payload = show_period(sdk, args.profile, args.period)
        elif args.verb == 'create':
            if not args.name:
                parser.error('--name is required')
            payload = create_partial(
                sdk, args.name, args.include_tag, timeout=args.timeout)
        else:
            if not args.name:
                parser.error('--name is required')
            payload = delete_named(sdk, args.name)
    except (ValueError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout)
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
