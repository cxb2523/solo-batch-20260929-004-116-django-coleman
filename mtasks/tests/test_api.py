import json
from unittest.mock import patch

import pytest

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from mtasks.models import Priority, State, Task


pytestmark = pytest.mark.django_db


@pytest.fixture()
def api_client():
    return APIClient()


User = get_user_model()


def results(data):
    return data['results'] if 'results' in data else data


def add_permission(user, codename):
    user.user_permissions.add(Permission.objects.get(content_type__app_label='mtasks', codename=codename))


def add_change_permission(user):
    add_permission(user, 'change_task')


def task_admin_group():
    group, _ = Group.objects.get_or_create(name='Task Admin')
    group.permissions.add(Permission.objects.get(content_type__app_label='mtasks', codename='view_task'))
    return group


def test_task_list_is_limited_to_own_tasks(api_client):
    owner = User.objects.create_user(username='owner', password='password')
    other = User.objects.create_user(username='other', password='password')
    add_permission(owner, 'view_task')
    own_task = Task.objects.create(title='Own task', user=owner, created_by=owner)
    Task.objects.create(title='Other task', user=other, created_by=other)

    api_client.force_authenticate(owner)
    response = api_client.get(reverse('task-list'), {'user': other.pk, 'state': State.TO_DO.value})

    assert response.status_code == status.HTTP_200_OK
    assert [task['number'] for task in results(response.data)] == [own_task.number]


def test_task_admin_can_list_all_tasks(api_client):
    admin = User.objects.create_user(username='admin', password='password')
    admin.groups.add(task_admin_group())
    owner = User.objects.create_user(username='owner', password='password')
    first = Task.objects.create(title='Admin visible', user=admin, created_by=admin)
    second = Task.objects.create(title='Owner visible', user=owner, created_by=admin)

    api_client.force_authenticate(admin)
    response = api_client.get(reverse('task-list'), {'user': owner.pk})

    assert response.status_code == status.HTTP_200_OK
    result_list = results(response.data)
    assert {task['number'] for task in result_list} == {first.number, second.number}


def test_anonymous_read_permission_remains_allowed(api_client):
    task = Task.objects.create(title='Public read', created_by=None)

    response = api_client.get(reverse('task-list'))

    assert response.status_code == status.HTTP_200_OK
    result_list = results(response.data)
    assert [task['number'] for task in result_list] == [task.number]


def test_owner_cannot_retrieve_another_users_task(api_client):
    owner = User.objects.create_user(username='owner', password='password')
    other = User.objects.create_user(username='other', password='password')
    add_permission(owner, 'view_task')
    task = Task.objects.create(title='Other task', user=other, created_by=other)

    api_client.force_authenticate(owner)
    response = api_client.get(reverse('task-detail', args=[task.pk]))

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_owner_cannot_update_another_users_task(api_client):
    owner = User.objects.create_user(username='owner', password='password')
    other = User.objects.create_user(username='other', password='password')
    add_permission(owner, 'view_task')
    add_change_permission(owner)
    task = Task.objects.create(title='Other task', user=other, created_by=other)

    api_client.force_authenticate(owner)
    response = api_client.patch(
        reverse('task-detail', args=[task.pk]),
        {'title': 'Hijacked title'},
        format='json',
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    task.refresh_from_db()
    assert task.title == 'Other task'


def test_update_audit_records_operator_and_values(api_client):
    operator = User.objects.create_user(username='operator', password='password')
    add_permission(operator, 'view_task')
    add_change_permission(operator)
    task = Task.objects.create(
        title='Before title',
        state=State.TO_DO.value,
        priority=Priority.NORMAL.value,
        user=operator,
        created_by=operator,
    )

    api_client.force_authenticate(operator)
    response = api_client.patch(
        reverse('task-detail', args=[task.pk]),
        {'title': 'After title', 'state': State.DONE.value},
        format='json',
    )

    assert response.status_code == status.HTTP_200_OK
    task.refresh_from_db()
    assert task.title == 'After title'
    assert task.state == State.DONE.value

    audit = LogEntry.objects.get(object_id=str(task.pk))
    assert audit.user == operator
    assert audit.action_flag == CHANGE
    assert audit.get_change_message() == 'Changed title and state.'
    change = json.loads(audit.change_message)[0]['changed']
    assert change['fields'] == ['title', 'state']
    assert change['old'] == {'title': 'Before title', 'state': State.TO_DO.value}
    assert change['new'] == {'title': 'After title', 'state': State.DONE.value}


def test_each_effective_update_adds_one_history_entry(api_client):
    owner = User.objects.create_user(username='owner', password='password')
    add_permission(owner, 'view_task')
    add_change_permission(owner)
    task = Task.objects.create(
        title='First title',
        state=State.TO_DO.value,
        priority=Priority.NORMAL.value,
        user=owner,
        created_by=owner,
    )
    api_client.force_authenticate(owner)

    first = api_client.patch(reverse('task-detail', args=[task.pk]), {'title': 'Second title'}, format='json')
    second = api_client.patch(reverse('task-detail', args=[task.pk]), {'state': State.DONE.value}, format='json')
    duplicate = api_client.patch(reverse('task-detail', args=[task.pk]), {'state': State.DONE.value}, format='json')

    assert first.status_code == status.HTTP_200_OK
    assert second.status_code == status.HTTP_200_OK
    assert duplicate.status_code == status.HTTP_200_OK
    assert LogEntry.objects.filter(object_id=str(task.pk)).count() == 2


def test_identical_update_does_not_write_audit(api_client):
    owner = User.objects.create_user(username='owner', password='password')
    add_permission(owner, 'view_task')
    add_change_permission(owner)
    task = Task.objects.create(
        title='Same title',
        state=State.TO_DO.value,
        priority=Priority.NORMAL.value,
        user=owner,
        created_by=owner,
    )

    api_client.force_authenticate(owner)
    response = api_client.patch(reverse('task-detail', args=[task.pk]), {'title': 'Same title'}, format='json')

    assert response.status_code == status.HTTP_200_OK
    assert LogEntry.objects.filter(object_id=str(task.pk)).count() == 0


def test_audit_failure_rolls_back_task_update(api_client):
    owner = User.objects.create_user(username='owner', password='password')
    add_permission(owner, 'view_task')
    add_change_permission(owner)
    task = Task.objects.create(
        title='Before title',
        priority=Priority.NORMAL.value,
        user=owner,
        created_by=owner,
    )

    api_client.force_authenticate(owner)
    with patch.object(LogEntry.objects, 'create', side_effect=RuntimeError('audit failed')):
        with pytest.raises(RuntimeError):
            api_client.patch(reverse('task-detail', args=[task.pk]), {'title': 'After title'}, format='json')

    task.refresh_from_db()
    assert task.title == 'Before title'
    assert LogEntry.objects.filter(object_id=str(task.pk)).count() == 0
