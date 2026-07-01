"""Skill version lifecycle backed by SQLite."""
from __future__ import annotations

import json
from typing import Any, Iterable

from memory import store

from .schema import SkillDocument, SkillStatus


class SkillNotFoundError(LookupError):
    pass


class SkillConflictError(ValueError):
    pass


class SkillRepository:
    def import_definitions(self, definitions: Iterable[Any]) -> int:
        """Import filesystem Skills once without overwriting editor-managed versions."""
        imported = 0
        for definition in definitions:
            document = getattr(definition, "document", None)
            if document is None:
                continue
            suffix = getattr(getattr(definition, "file_path", None), "suffix", "")
            source_format = "yaml" if suffix.lower() in {".yaml", ".yml"} else "markdown"
            try:
                self.create(document, source_format=source_format)
                imported += 1
            except SkillConflictError:
                continue
        return imported

    def create(self, document: SkillDocument, source_format: str = "yaml") -> dict[str, Any]:
        if document.metadata.status != SkillStatus.DRAFT:
            raise SkillConflictError("A new Skill version must start as draft")
        try:
            record = store.create_skill_version(
                skill_id=document.metadata.id,
                name=document.metadata.name,
                description=document.metadata.description,
                version=document.metadata.version,
                status=document.metadata.status.value,
                document=document.model_dump(mode="json", by_alias=True, exclude_none=True),
                source_format=source_format,
            )
        except ValueError as error:
            raise SkillConflictError(str(error)) from error
        return self._version_dict(record)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": record.id,
                "name": record.name,
                "description": record.description,
                "latestVersion": record.latest_version,
                "publishedVersion": record.published_version,
                "createdAt": record.created_at.isoformat(),
                "updatedAt": record.updated_at.isoformat(),
            }
            for record in store.list_skill_records()
        ]

    def get(self, skill_id: str) -> dict[str, Any]:
        record = store.get_skill_record(skill_id)
        if record is None:
            raise SkillNotFoundError(f"Skill not found: {skill_id}")
        return {
            "id": record.id,
            "name": record.name,
            "description": record.description,
            "latestVersion": record.latest_version,
            "publishedVersion": record.published_version,
            "versions": [self._version_dict(item) for item in store.list_skill_versions(skill_id)],
        }

    def get_version(self, skill_id: str, version: str) -> dict[str, Any]:
        record = store.get_skill_version(skill_id, version)
        if record is None:
            raise SkillNotFoundError(f"Skill version not found: {skill_id}@{version}")
        return self._version_dict(record)

    def update_draft(self, skill_id: str, version: str, document: SkillDocument) -> dict[str, Any]:
        current = self._get_record(skill_id, version)
        if current.status != SkillStatus.DRAFT.value:
            raise SkillConflictError("Only draft Skill versions can be edited")
        if document.metadata.id != skill_id or document.metadata.version != version:
            raise SkillConflictError("Skill id and version cannot be changed in place")
        document.metadata.status = SkillStatus.DRAFT
        updated = store.update_skill_version(
            skill_id,
            version,
            document=document.model_dump(mode="json", by_alias=True, exclude_none=True),
            name=document.metadata.name,
            description=document.metadata.description,
        )
        return self._version_dict(updated)

    def validate(self, skill_id: str, version: str) -> dict[str, Any]:
        current = self._get_record(skill_id, version)
        if current.status not in {SkillStatus.DRAFT.value, SkillStatus.TESTING.value}:
            raise SkillConflictError("Only draft or testing versions can be validated")
        document = SkillDocument.model_validate_json(current.document)
        document.metadata.status = SkillStatus.VALIDATED
        updated = store.update_skill_version(
            skill_id,
            version,
            status=SkillStatus.VALIDATED.value,
            document=document.model_dump(mode="json", by_alias=True, exclude_none=True),
            validated=True,
        )
        return self._version_dict(updated)

    def publish(self, skill_id: str, version: str) -> dict[str, Any]:
        current = self._get_record(skill_id, version)
        if current.status != SkillStatus.VALIDATED.value:
            raise SkillConflictError("Only validated Skill versions can be published")
        document = SkillDocument.model_validate_json(current.document)
        document.metadata.status = SkillStatus.PUBLISHED
        updated = store.update_skill_version(
            skill_id,
            version,
            status=SkillStatus.PUBLISHED.value,
            document=document.model_dump(mode="json", by_alias=True, exclude_none=True),
            published=True,
        )
        return self._version_dict(updated)

    def deprecate(self, skill_id: str, version: str) -> dict[str, Any]:
        current = self._get_record(skill_id, version)
        if current.status != SkillStatus.PUBLISHED.value:
            raise SkillConflictError("Only a published Skill version can be deprecated")
        document = SkillDocument.model_validate_json(current.document)
        document.metadata.status = SkillStatus.DEPRECATED
        updated = store.update_skill_version(
            skill_id,
            version,
            status=SkillStatus.DEPRECATED.value,
            document=document.model_dump(mode="json", by_alias=True, exclude_none=True),
            deprecated=True,
        )
        return self._version_dict(updated)

    @staticmethod
    def _version_dict(record) -> dict[str, Any]:
        return {
            "skillId": record.skill_id,
            "version": record.version,
            "status": record.status,
            "sourceFormat": record.source_format,
            "document": json.loads(record.document),
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "validatedAt": record.validated_at.isoformat() if record.validated_at else None,
            "publishedAt": record.published_at.isoformat() if record.published_at else None,
        }

    @staticmethod
    def _get_record(skill_id: str, version: str):
        record = store.get_skill_version(skill_id, version)
        if record is None:
            raise SkillNotFoundError(f"Skill version not found: {skill_id}@{version}")
        return record
