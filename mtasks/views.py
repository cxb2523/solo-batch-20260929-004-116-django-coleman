import json

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import response, viewsets

from .models import Task
from .serializers import TaskSerializer


TASK_ADMIN_GROUP = 'Task Admin'


class TaskViewSet(viewsets.ModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer

    def get_queryset(self):
        queryset = Task.objects.all()
        user = self.request.user

        if not user.is_authenticated or user.is_superuser or self._is_task_admin(user):
            return queryset

        return queryset.filter(user=user)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)

        with transaction.atomic():
            instance = self._get_locked_object()
            old_values = self._snapshot_values(instance)

            serializer = self.get_serializer(instance, data=request.data, partial=partial)
            serializer.is_valid(raise_exception=True)
            self.perform_update(serializer)

            changes = self._build_changes(instance, old_values, serializer.validated_data)
            if changes:
                self._write_change_audit(request.user, instance, changes)

            return response.Response(serializer.data)

    def _is_task_admin(self, user):
        return user.groups.filter(name=TASK_ADMIN_GROUP).exists()

    def _get_locked_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        filter_kwargs = {self.lookup_field: self.kwargs[lookup_url_kwarg]}
        instance = get_object_or_404(queryset.select_for_update(), **filter_kwargs)
        self.check_object_permissions(self.request, instance)
        return instance

    def _snapshot_values(self, instance):
        return {
            field_name: self._raw_field_value(instance, field_name)
            for field_name in self._writable_model_fields()
        }

    def _writable_model_fields(self):
        serializer = self.get_serializer()
        field_names = []

        for field_name, serializer_field in serializer.fields.items():
            if serializer_field.read_only:
                continue
            try:
                Task._meta.get_field(field_name)
            except FieldDoesNotExist:
                continue
            field_names.append(field_name)

        return field_names

    def _build_changes(self, instance, old_values, validated_data):
        field_names = []
        old_data = {}
        new_data = {}
        old_display = {}
        new_display = {}

        for field_name in validated_data.keys():
            if field_name not in old_values:
                continue

            old_value = old_values[field_name]
            new_value = self._raw_field_value(instance, field_name)
            if old_value == new_value:
                continue

            field_names.append(field_name)
            old_data[field_name] = old_value
            new_data[field_name] = new_value
            old_display[field_name] = self._display_field_value(instance, field_name, old_value)
            new_display[field_name] = self._display_field_value(instance, field_name, new_value)

        if not field_names:
            return []

        return [
            {
                'changed': {
                    'fields': field_names,
                    'old': old_data,
                    'new': new_data,
                    'old_display': old_display,
                    'new_display': new_display,
                }
            }
        ]

    def _raw_field_value(self, instance, field_name):
        field = Task._meta.get_field(field_name)
        if field.is_relation:
            return getattr(instance, field.attname)

        value = getattr(instance, field_name)
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        return value

    def _display_field_value(self, instance, field_name, value):
        field = Task._meta.get_field(field_name)
        if field.choices:
            return str(dict(field.flatchoices).get(value, value))
        if field.is_relation:
            related = getattr(instance, field_name)
            return str(related) if related is not None else None
        return value if value is None else str(value)

    def _write_change_audit(self, user, instance, changes):
        LogEntry.objects.create(
            user=user,
            content_type=ContentType.objects.get_for_model(Task, for_concrete_model=False),
            object_id=str(instance.pk),
            object_repr=str(instance)[:200],
            action_flag=CHANGE,
            change_message=json.dumps(changes, ensure_ascii=False),
        )
