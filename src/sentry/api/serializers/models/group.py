from __future__ import absolute_import, print_function

from collections import defaultdict
from datetime import timedelta
from itertools import izip

import six
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.utils import timezone

from sentry import tagstore, tsdb
from sentry.api.serializers import Serializer, register, serialize
from sentry.constants import LOG_LEVELS, StatsPeriod
from sentry.models import (
    Group, GroupAssignee, GroupBookmark, GroupMeta, GroupResolution, GroupSeen, GroupSnooze,
    GroupShare, GroupStatus, GroupSubscription, GroupSubscriptionReason, User, UserOption,
    UserOptionValue
)
from sentry.utils.db import attach_foreignkey
from sentry.utils.http import absolute_uri
from sentry.utils.safe import safe_execute

SUBSCRIPTION_REASON_MAP = {
    GroupSubscriptionReason.comment: 'commented',
    GroupSubscriptionReason.assigned: 'assigned',
    GroupSubscriptionReason.bookmark: 'bookmarked',
    GroupSubscriptionReason.status_change: 'changed_status',
    GroupSubscriptionReason.mentioned: 'mentioned',
}


@register(Group)
class GroupSerializer(Serializer):
    def _get_subscriptions(self, item_list, user):
        """
        Returns a mapping of group IDs to a two-tuple of (subscribed: bool,
        subscription: GroupSubscription or None) for the provided user and
        groups.
        """
        results = {group.id: None for group in item_list}

        # First, the easy part -- if there is a subscription record associated
        # with the group, we can just use that to know if a user is subscribed
        # or not.
        subscriptions = GroupSubscription.objects.filter(
            group__in=results.keys(),
            user=user,
        )

        for subscription in subscriptions:
            results[subscription.group_id] = (subscription.is_active, subscription)

        # For any group that doesn't have a subscription associated with it,
        # we'll need to fall back to the project's option value, so here we
        # collect all of the projects to look up, and keep a set of groups that
        # are part of that project. (Note that the common -- but not only --
        # case here is that all groups are part of the same project.)
        projects = defaultdict(set)
        for group in item_list:
            if results[group.id] is None:
                projects[group.project].add(group.id)

        if projects:
            # NOTE: This doesn't use `values_list` because that bypasses field
            # value decoding, so the `value` field would not be unpickled.
            options = {
                option.project_id: option.value
                for option in UserOption.objects.filter(
                    Q(project__in=projects.keys()) | Q(project__isnull=True),
                    user=user,
                    key='workflow:notifications',
                )
            }

            # This is the user's default value for any projects that don't have
            # the option value specifically recorded. (The default "all
            # conversations" value is convention.)
            default = options.get(None, UserOptionValue.all_conversations)

            # If you're subscribed to all notifications for the project, that
            # means you're subscribed to all of the groups. Otherwise you're
            # not subscribed to any of these leftover groups.
            for project, group_ids in projects.items():
                is_subscribed = options.get(
                    project.id,
                    default,
                ) == UserOptionValue.all_conversations
                for group_id in group_ids:
                    results[group_id] = (is_subscribed, None)

        return results

    def get_attrs(self, item_list, user):
        from sentry.plugins import plugins

        GroupMeta.objects.populate_cache(item_list)

        attach_foreignkey(item_list, Group.project)

        if user.is_authenticated() and item_list:
            bookmarks = set(
                GroupBookmark.objects.filter(
                    user=user,
                    group__in=item_list,
                ).values_list('group_id', flat=True)
            )
            seen_groups = dict(
                GroupSeen.objects.filter(
                    user=user,
                    group__in=item_list,
                ).values_list('group_id', 'last_seen')
            )
            subscriptions = self._get_subscriptions(item_list, user)
        else:
            bookmarks = set()
            seen_groups = {}
            subscriptions = defaultdict(lambda: (False, None))

        assignees = dict(
            (a.group_id, a.user)
            for a in GroupAssignee.objects.filter(
                group__in=item_list,
            ).select_related('user')
        )

        user_counts = tagstore.get_group_values_seen([g.id for g in item_list], 'sentry:user')

        ignore_items = {g.group_id: g for g in GroupSnooze.objects.filter(
            group__in=item_list,
        )}

        resolutions = {
            i[0]: i[1:]
            for i in GroupResolution.objects.filter(
                group__in=item_list,
            ).values_list(
                'group',
                'type',
                'release__version',
                'actor_id',
            )
        }
        actor_ids = set(r[-1] for r in six.itervalues(resolutions))
        actor_ids.update(r.actor_id for r in six.itervalues(ignore_items))
        if actor_ids:
            users = list(User.objects.filter(
                id__in=actor_ids,
                is_active=True,
            ))
            actors = {u.id: d for u, d in izip(users, serialize(users, user))}
        else:
            actors = {}

        share_ids = dict(GroupShare.objects.filter(
            group__in=item_list,
        ).values_list('group_id', 'uuid'))

        result = {}
        for item in item_list:
            active_date = item.active_at or item.first_seen

            annotations = []
            for plugin in plugins.for_project(project=item.project, version=1):
                safe_execute(plugin.tags, None, item, annotations, _with_transaction=False)
            for plugin in plugins.for_project(project=item.project, version=2):
                annotations.extend(
                    safe_execute(plugin.get_annotations, group=item, _with_transaction=False) or ()
                )

            resolution = resolutions.get(item.id)
            if resolution:
                resolution_actor = actors.get(resolution[-1])
            else:
                resolution_actor = None

            ignore_item = ignore_items.get(item.id)
            if ignore_item:
                ignore_actor = actors.get(ignore_item.actor_id)
            else:
                ignore_actor = None

            result[item] = {
                'assigned_to': serialize(assignees.get(item.id)),
                'is_bookmarked': item.id in bookmarks,
                'subscription': subscriptions[item.id],
                'has_seen': seen_groups.get(item.id, active_date) > active_date,
                'annotations': annotations,
                'user_count': user_counts.get(item.id, 0),
                'ignore_until': ignore_item,
                'ignore_actor': ignore_actor,
                'resolution': resolution,
                'resolution_actor': resolution_actor,
                'share_id': share_ids.get(item.id),
            }
        return result

    def serialize(self, obj, attrs, user):
        status = obj.status
        status_details = {}
        if attrs['ignore_until']:
            snooze = attrs['ignore_until']
            if snooze.is_valid(group=obj):
                # counts return the delta remaining when window is not set
                status_details.update(
                    {
                        'ignoreCount': (
                            snooze.count - (obj.times_seen - snooze.state['times_seen'])
                            if snooze.count and not snooze.window else snooze.count
                        ),
                        'ignoreUntil':
                        snooze.until,
                        'ignoreUserCount': (
                            snooze.user_count - (attrs['user_count'] - snooze.state['users_seen'])
                            if snooze.user_count and not snooze.user_window else snooze.user_count
                        ),
                        'ignoreUserWindow':
                        snooze.user_window,
                        'ignoreWindow':
                        snooze.window,
                        'actor':
                        attrs['ignore_actor'],
                    }
                )
            else:
                status = GroupStatus.UNRESOLVED
        if status == GroupStatus.UNRESOLVED and obj.is_over_resolve_age():
            status = GroupStatus.RESOLVED
            status_details['autoResolved'] = True
        if status == GroupStatus.RESOLVED:
            status_label = 'resolved'
            if attrs['resolution']:
                res_type, res_version, _ = attrs['resolution']
                if res_type in (GroupResolution.Type.in_next_release, None):
                    status_details['inNextRelease'] = True
                elif res_type == GroupResolution.Type.in_release:
                    status_details['inRelease'] = res_version
                status_details['actor'] = attrs['resolution_actor']
        elif status == GroupStatus.IGNORED:
            status_label = 'ignored'
        elif status in [GroupStatus.PENDING_DELETION, GroupStatus.DELETION_IN_PROGRESS]:
            status_label = 'pending_deletion'
        elif status == GroupStatus.PENDING_MERGE:
            status_label = 'pending_merge'
        else:
            status_label = 'unresolved'

        # If user is not logged in and member of the organization,
        # do not return the permalink which contains private information i.e. org name.
        if user.is_authenticated() and user.get_orgs().filter(id=obj.organization.id).exists():
            permalink = absolute_uri(
                reverse('sentry-group', args=[obj.organization.slug, obj.project.slug, obj.id])
            )
        else:
            permalink = None

        is_subscribed, subscription = attrs['subscription']
        share_id = attrs['share_id']

        return {
            'id': six.text_type(obj.id),
            'shareId': share_id,
            'shortId': obj.qualified_short_id,
            'count': six.text_type(obj.times_seen),
            'userCount': attrs['user_count'],
            'title': obj.title,
            'culprit': obj.culprit,
            'permalink': permalink,
            'firstSeen': obj.first_seen,
            'lastSeen': obj.last_seen,
            'logger': obj.logger or None,
            'level': LOG_LEVELS.get(obj.level, 'unknown'),
            'status': status_label,
            'statusDetails': status_details,
            'isPublic': share_id is not None,
            'project': {
                'name': obj.project.name,
                'slug': obj.project.slug,
            },
            'type': obj.get_event_type(),
            'metadata': obj.get_event_metadata(),
            'numComments': obj.num_comments,
            'assignedTo': attrs['assigned_to'],
            'isBookmarked': attrs['is_bookmarked'],
            'isSubscribed': is_subscribed,
            'subscriptionDetails': {
                'reason': SUBSCRIPTION_REASON_MAP.get(
                    subscription.reason,
                    'unknown',
                ),
            } if is_subscribed and subscription is not None else None,
            'hasSeen': attrs['has_seen'],
            'annotations': attrs['annotations'],
        }


class StreamGroupSerializer(GroupSerializer):
    STATS_PERIOD_CHOICES = {
        '14d': StatsPeriod(14, timedelta(hours=24)),
        '24h': StatsPeriod(24, timedelta(hours=1)),
    }

    def __init__(self, stats_period=None, matching_event_id=None):
        if stats_period is not None:
            assert stats_period in self.STATS_PERIOD_CHOICES

        self.stats_period = stats_period
        self.matching_event_id = matching_event_id

    def get_attrs(self, item_list, user):
        attrs = super(StreamGroupSerializer, self).get_attrs(item_list, user)

        if self.stats_period:
            # we need to compute stats at 1d (1h resolution), and 14d
            group_ids = [g.id for g in item_list]

            segments, interval = self.STATS_PERIOD_CHOICES[self.stats_period]
            now = timezone.now()
            stats = tsdb.get_range(
                model=tsdb.models.group,
                keys=group_ids,
                end=now,
                start=now - ((segments - 1) * interval),
                rollup=int(interval.total_seconds()),
            )

            for item in item_list:
                attrs[item].update({
                    'stats': stats[item.id],
                })

        return attrs

    def serialize(self, obj, attrs, user):
        result = super(StreamGroupSerializer, self).serialize(obj, attrs, user)

        if self.stats_period:
            result['stats'] = {
                self.stats_period: attrs['stats'],
            }

        if self.matching_event_id:
            result['matchingEventId'] = self.matching_event_id

        return result


class TagBasedStreamGroupSerializer(StreamGroupSerializer):
    def __init__(self, tags, **kwargs):
        super(TagBasedStreamGroupSerializer, self).__init__(**kwargs)
        self.tags = tags

    def serialize(self, obj, attrs, user):
        result = super(TagBasedStreamGroupSerializer, self).serialize(obj, attrs, user)
        result['tagLastSeen'] = self.tags[obj.id].last_seen
        result['tagFirstSeen'] = self.tags[obj.id].first_seen
        return result


class SharedGroupSerializer(GroupSerializer):
    def serialize(self, obj, attrs, user):
        result = super(SharedGroupSerializer, self).serialize(obj, attrs, user)
        del result['annotations']
        return result
