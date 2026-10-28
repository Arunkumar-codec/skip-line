"""
app/services/form_service.py
────────────────────────────
Dynamic Custom Form Builder and Submission Engine.
Features:
- Security validation: Strict prohibition of credential/payment card collection (FORM-007)
- Form definition CRUD, field options, and conditional display rules
- Form versioning and publish status
- Submission validation with dynamic condition evaluation
- Immutable schema snapshotting and answer persistence for token bookings
"""

import json
import re
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.custom_form import (
    CustomForm,
    FormField,
    FormFieldCondition,
    FormFieldOption,
    TokenFormAnswer,
    TokenFormSnapshot,
)
from models.service import Service

# FORM-007: Forbidden field terms (passwords, PINs, OTPs, card credentials)
PROHIBITED_FIELD_PATTERNS = [
    r"\bpassword\b",
    r"\bpasswd\b",
    r"\bpin\b",
    r"\botp\b",
    r"\bone[-_ ]time[-_ ]password\b",
    r"\bcvv\b",
    r"\bcvc\b",
    r"\bcredit[-_ ]card\b",
    r"\bdebit[-_ ]card\b",
    r"\bcard[-_ ]number\b",
    r"\bcard[-_ ]pin\b",
    r"\bbank[-_ ]account[-_ ]password\b",
    r"\bsecurity[-_ ]code\b",
    r"\bupi[-_ ]pin\b",
    r"\bnetbanking[-_ ]password\b",
]

COMPILED_PROHIBITED = [re.compile(p, re.IGNORECASE) for p in PROHIBITED_FIELD_PATTERNS]


def validate_prohibited_terms(text: str) -> None:
    """Validate that text does not contain prohibited credential or financial terms."""
    if not text:
        return
    normalized = re.sub(r"[-_]+", " ", text)
    for pattern in COMPILED_PROHIBITED:
        if pattern.search(text) or pattern.search(normalized):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Prohibited field detected: Custom forms must not collect sensitive credentials, "
                    "passwords, PINs, OTPs, or payment card details (Rule FORM-007)."
                ),
            )


def validate_field_definition(field_data: dict[str, Any]) -> None:
    """Check field key, label, placeholder, and help_text for prohibited terms."""
    validate_prohibited_terms(field_data.get("field_key", ""))
    validate_prohibited_terms(field_data.get("label", ""))
    validate_prohibited_terms(field_data.get("placeholder", ""))
    validate_prohibited_terms(field_data.get("help_text", ""))


async def get_form_by_id(
    db: AsyncSession, form_id: int, load_relationships: bool = True
) -> Optional[CustomForm]:
    """Retrieve custom form by ID with nested fields, options, and conditions."""
    if not load_relationships:
        return await db.get(CustomForm, form_id)

    stmt = (
        select(CustomForm)
        .where(CustomForm.id == form_id)
        .options(
            selectinload(CustomForm.fields).selectinload(FormField.options),
            selectinload(CustomForm.fields).selectinload(FormField.conditions),
        )
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_active_form_for_service(
    db: AsyncSession, service_id: int
) -> Optional[CustomForm]:
    """Retrieve published, active custom form for a specific service."""
    stmt = (
        select(CustomForm)
        .where(
            CustomForm.service_id == service_id,
            CustomForm.is_published.is_(True),
            CustomForm.is_active.is_(True),
        )
        .options(
            selectinload(CustomForm.fields).selectinload(FormField.options),
            selectinload(CustomForm.fields).selectinload(FormField.conditions),
        )
        .order_by(desc(CustomForm.version))
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def list_forms_by_org(
    db: AsyncSession,
    organization_id: int,
    service_id: Optional[int] = None,
) -> list[CustomForm]:
    """List custom forms for an organization."""
    stmt = (
        select(CustomForm)
        .where(CustomForm.organization_id == organization_id)
        .options(
            selectinload(CustomForm.fields).selectinload(FormField.options),
            selectinload(CustomForm.fields).selectinload(FormField.conditions),
        )
    )
    if service_id is not None:
        stmt = stmt.where(CustomForm.service_id == service_id)

    stmt = stmt.order_by(desc(CustomForm.updated_at))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_form(
    db: AsyncSession,
    organization_id: int,
    title: str,
    description: Optional[str] = None,
    service_id: Optional[int] = None,
    fields_data: Optional[list[dict[str, Any]]] = None,
    is_published: bool = False,
) -> CustomForm:
    """Create a new custom form with fields, options, and conditions."""
    if service_id is not None:
        service = await db.get(Service, service_id)
        if not service or service.organization_id != organization_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Linked service does not belong to this organization.",
            )

    form = CustomForm(
        organization_id=organization_id,
        service_id=service_id,
        title=title,
        description=description,
        version=1,
        is_published=is_published,
        is_active=True,
    )
    db.add(form)
    await db.flush()

    if fields_data:
        created_fields: list[tuple[FormField, dict[str, Any]]] = []
        field_key_map: dict[str, int] = {}

        for idx, f_data in enumerate(fields_data):
            validate_field_definition(f_data)
            field = FormField(
                form_id=form.id,
                field_key=f_data["field_key"],
                label=f_data["label"],
                field_type=f_data.get("field_type", "text"),
                placeholder=f_data.get("placeholder"),
                help_text=f_data.get("help_text"),
                is_required=bool(f_data.get("is_required", False)),
                validation_regex=f_data.get("validation_regex"),
                order_index=f_data.get("order_index", idx),
            )
            db.add(field)
            await db.flush()
            field_key_map[field.field_key] = field.id
            created_fields.append((field, f_data))

            for opt_idx, opt_data in enumerate(f_data.get("options", [])):
                option = FormFieldOption(
                    field_id=field.id,
                    option_label=opt_data["option_label"],
                    option_value=opt_data["option_value"],
                    order_index=opt_data.get("order_index", opt_idx),
                )
                db.add(option)

        # Save conditions
        for field, f_data in created_fields:
            for cond_data in f_data.get("conditions", []):
                dep_id = cond_data.get("depends_on_field_id")
                dep_key = cond_data.get("depends_on_field_key")
                if not dep_id and dep_key and dep_key in field_key_map:
                    dep_id = field_key_map[dep_key]
                if dep_id:
                    cond = FormFieldCondition(
                        field_id=field.id,
                        depends_on_field_id=dep_id,
                        operator=cond_data["operator"],
                        trigger_value=str(cond_data["trigger_value"]),
                        action=cond_data.get("action", "show"),
                    )
                    db.add(cond)

    form_id = form.id
    await db.commit()
    return await get_form_by_id(db, form_id)  # type: ignore


async def update_form(
    db: AsyncSession,
    organization_id: int,
    form_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    service_id: Optional[int] = None,
    fields_data: Optional[list[dict[str, Any]]] = None,
    is_published: Optional[bool] = None,
) -> CustomForm:
    """
    Update an existing custom form.
    Increments version when published schema changes.
    """
    form = await get_form_by_id(db, form_id)
    if not form or form.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Form not found",
        )

    if service_id is not None:
        service = await db.get(Service, service_id)
        if not service or service.organization_id != organization_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Linked service does not belong to this organization.",
            )

    if title is not None:
        form.title = title
    if description is not None:
        form.description = description
    if service_id is not None:
        form.service_id = service_id
    if is_published is not None:
        form.is_published = is_published

    if fields_data is not None:
        form.version += 1
        # Clear existing fields (cascade deletes options and conditions)
        form.fields.clear()
        await db.flush()

        created_fields: list[tuple[FormField, dict[str, Any]]] = []
        field_key_map: dict[str, int] = {}

        for idx, f_data in enumerate(fields_data):
            validate_field_definition(f_data)
            field = FormField(
                form_id=form.id,
                field_key=f_data["field_key"],
                label=f_data["label"],
                field_type=f_data.get("field_type", "text"),
                placeholder=f_data.get("placeholder"),
                help_text=f_data.get("help_text"),
                is_required=bool(f_data.get("is_required", False)),
                validation_regex=f_data.get("validation_regex"),
                order_index=f_data.get("order_index", idx),
            )
            form.fields.append(field)
            await db.flush()
            field_key_map[field.field_key] = field.id
            created_fields.append((field, f_data))

            for opt_idx, opt_data in enumerate(f_data.get("options", [])):
                option = FormFieldOption(
                    field_id=field.id,
                    option_label=opt_data["option_label"],
                    option_value=opt_data["option_value"],
                    order_index=opt_data.get("order_index", opt_idx),
                )
                field.options.append(option)

        for field, f_data in created_fields:
            for cond_data in f_data.get("conditions", []):
                dep_id = cond_data.get("depends_on_field_id")
                dep_key = cond_data.get("depends_on_field_key")
                if not dep_id and dep_key and dep_key in field_key_map:
                    dep_id = field_key_map[dep_key]
                if dep_id:
                    cond = FormFieldCondition(
                        field_id=field.id,
                        depends_on_field_id=dep_id,
                        operator=cond_data["operator"],
                        trigger_value=str(cond_data["trigger_value"]),
                        action=cond_data.get("action", "show"),
                    )
                    field.conditions.append(cond)

    await db.commit()
    return await get_form_by_id(db, form_id)  # type: ignore


async def delete_form(
    db: AsyncSession,
    organization_id: int,
    form_id: int,
) -> bool:
    """Soft delete or remove a custom form."""
    form = await db.get(CustomForm, form_id)
    if not form or form.organization_id != organization_id:
        return False
    await db.delete(form)
    await db.commit()
    return True


async def retire_form(
    db: AsyncSession,
    organization_id: int,
    form_id: int,
) -> CustomForm:
    """Retire a form so it is no longer active or published."""
    form = await get_form_by_id(db, form_id)
    if not form or form.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Form not found",
        )
    form.is_active = False
    form.is_published = False
    await db.commit()
    return await get_form_by_id(db, form_id)


async def duplicate_form(
    db: AsyncSession,
    organization_id: int,
    form_id: int,
) -> CustomForm:
    """Duplicate an existing custom form into a draft copy with version 1."""
    original = await get_form_by_id(db, form_id)
    if not original or original.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Form not found",
        )

    new_form = CustomForm(
        organization_id=organization_id,
        service_id=original.service_id,
        title=f"{original.title} (Copy)",
        description=original.description,
        version=1,
        is_published=False,
        is_active=True,
    )
    db.add(new_form)
    await db.flush()

    old_to_new_field_map: dict[int, int] = {}
    pending_conditions: list[tuple[int, FormFieldCondition]] = []

    for f in original.fields:
        new_field = FormField(
            form_id=new_form.id,
            field_key=f.field_key,
            label=f.label,
            field_type=f.field_type,
            placeholder=f.placeholder,
            help_text=f.help_text,
            is_required=f.is_required,
            validation_regex=f.validation_regex,
            order_index=f.order_index,
        )
        db.add(new_field)
        await db.flush()
        old_to_new_field_map[f.id] = new_field.id

        for opt in f.options:
            new_opt = FormFieldOption(
                field_id=new_field.id,
                option_label=opt.option_label,
                option_value=opt.option_value,
                order_index=opt.order_index,
            )
            db.add(new_opt)

        for cond in f.conditions:
            pending_conditions.append((new_field.id, cond))

    for new_field_id, cond in pending_conditions:
        if cond.depends_on_field_id in old_to_new_field_map:
            new_cond = FormFieldCondition(
                field_id=new_field_id,
                depends_on_field_id=old_to_new_field_map[cond.depends_on_field_id],
                operator=cond.operator,
                trigger_value=cond.trigger_value,
                action=cond.action,
            )
            db.add(new_cond)

    new_form_id = new_form.id
    await db.commit()
    return await get_form_by_id(db, new_form_id)  # type: ignore


def serialize_form_schema(form: CustomForm) -> dict[str, Any]:
    """Export complete immutable form definition dictionary for JSON snapshotting."""
    fields_repr = []
    for f in form.fields:
        opts_repr = [{"label": o.option_label, "value": o.option_value} for o in f.options]
        conds_repr = [
            {
                "depends_on_field_id": c.depends_on_field_id,
                "operator": c.operator,
                "trigger_value": c.trigger_value,
                "action": c.action,
            }
            for c in f.conditions
        ]
        fields_repr.append({
            "id": f.id,
            "field_key": f.field_key,
            "label": f.label,
            "field_type": f.field_type,
            "placeholder": f.placeholder,
            "help_text": f.help_text,
            "is_required": f.is_required,
            "validation_regex": f.validation_regex,
            "order_index": f.order_index,
            "options": opts_repr,
            "conditions": conds_repr,
        })

    return {
        "form_id": form.id,
        "organization_id": form.organization_id,
        "service_id": form.service_id,
        "title": form.title,
        "description": form.description,
        "version": form.version,
        "fields": fields_repr,
    }


def evaluate_condition(operator: str, val: str, target: str) -> bool:
    op = operator.lower()
    if op == "equals":
        return val.lower() == target.lower()
    elif op == "not_equals":
        return val.lower() != target.lower()
    elif op == "contains":
        return target.lower() in val.lower()
    elif op == "greater_than":
        try:
            return float(val) > float(target)
        except (ValueError, TypeError):
            return False
    elif op == "less_than":
        try:
            return float(val) < float(target)
        except (ValueError, TypeError):
            return False
    return False


def validate_form_submission(
    form: CustomForm, answers: dict[str, Any]
) -> dict[str, str]:
    """
    Validate customer answers against form fields, regex constraints, and conditional rules.
    Returns sanitized answers dictionary {field_key: string_value}.
    """
    sanitized: dict[str, str] = {}
    for f in form.fields:
        raw_val = answers.get(f.field_key)
        val_str = str(raw_val).strip() if raw_val is not None else ""

        # Check conditions
        is_visible = True
        field_required = f.is_required

        if f.conditions:
            for cond in f.conditions:
                dep_field = next((df for df in form.fields if df.id == cond.depends_on_field_id), None)
                if dep_field:
                    dep_val = str(answers.get(dep_field.field_key, "")).strip()
                    matched = evaluate_condition(cond.operator, dep_val, cond.trigger_value)
                    if cond.action == "show":
                        if not matched:
                            is_visible = False
                    elif cond.action == "hide":
                        if matched:
                            is_visible = False
                    elif cond.action == "require":
                        if matched:
                            field_required = True

        if not is_visible:
            # If conditionally hidden, do not enforce required validation
            sanitized[f.field_key] = val_str
            continue

        if field_required and not val_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Field '{f.field_key}' is required ({f.label})." if f.label else f"Field '{f.field_key}' is required.",
            )

        if val_str and f.validation_regex:
            try:
                if not re.search(f.validation_regex, val_str):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Field '{f.field_key}' does not match required format.",
                    )
            except re.error:
                pass  # Ignore invalid regex patterns stored

        sanitized[f.field_key] = val_str

    return sanitized


async def create_token_form_snapshot(
    db: AsyncSession,
    token_id: int,
    form: CustomForm,
    raw_answers: dict[str, Any],
) -> TokenFormSnapshot:
    """
    Capture immutable schema snapshot and answers for a token booking.
    Freezes form schema as JSON.
    """
    sanitized_answers = validate_form_submission(form, raw_answers)
    schema_dict = serialize_form_schema(form)
    schema_json = json.dumps(schema_dict)

    snapshot = TokenFormSnapshot(
        token_id=token_id,
        form_id=form.id,
        form_title=form.title,
        form_version=form.version,
        schema_snapshot=schema_json,
    )
    db.add(snapshot)
    await db.flush()

    # Save answers
    for f in form.fields:
        ans_val = sanitized_answers.get(f.field_key, "")
        ans = TokenFormAnswer(
            snapshot_id=snapshot.id,
            field_key=f.field_key,
            field_label=f.label,
            field_type=f.field_type,
            answer_value=ans_val,
        )
        db.add(ans)

    await db.flush()
    return snapshot
