"""app/schemas/form.py"""

from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


class FormFieldOptionCreate(BaseModel):
    option_label: str = Field(..., max_length=200)
    option_value: str = Field(..., max_length=200)
    order_index: int = 0


class FormFieldOptionRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    option_label: str
    option_value: str
    order_index: int


class FormFieldConditionCreate(BaseModel):
    depends_on_field_id: Optional[int] = None
    depends_on_field_key: Optional[str] = None
    operator: str = Field(..., max_length=50)  # equals, not_equals, contains, greater_than, less_than
    trigger_value: str = Field(..., max_length=255)
    action: str = Field("show", max_length=50)  # show, hide, require


class FormFieldConditionRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    field_id: int
    depends_on_field_id: int
    operator: str
    trigger_value: str
    action: str


class FormFieldCreate(BaseModel):
    field_key: str = Field(..., max_length=100)
    label: str = Field(..., max_length=200)
    field_type: str = Field("text", max_length=50)  # text, number, select, radio, checkbox, textarea, date, file
    placeholder: Optional[str] = Field(None, max_length=200)
    help_text: Optional[str] = None
    is_required: bool = False
    validation_regex: Optional[str] = Field(None, max_length=255)
    order_index: int = 0
    options: list[FormFieldOptionCreate] = []
    conditions: list[FormFieldConditionCreate] = []


class FormFieldRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    field_key: str
    label: str
    field_type: str
    placeholder: Optional[str]
    help_text: Optional[str]
    is_required: bool
    validation_regex: Optional[str]
    order_index: int
    options: list[FormFieldOptionRead] = []
    conditions: list[FormFieldConditionRead] = []


class CustomFormCreate(BaseModel):
    title: str = Field(..., max_length=200)
    description: Optional[str] = None
    service_id: Optional[int] = None
    is_published: bool = False
    fields: list[FormFieldCreate] = []


class CustomFormUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    service_id: Optional[int] = None
    is_published: Optional[bool] = None
    fields: Optional[list[FormFieldCreate]] = None


class CustomFormRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: int
    service_id: Optional[int]
    title: str
    description: Optional[str]
    version: int
    is_published: bool
    is_active: bool
    fields: list[FormFieldRead] = []
    created_at: datetime
    updated_at: datetime


class TokenFormAnswerRead(BaseModel):
    model_config = {"from_attributes": True}

    field_key: str
    field_label: str
    field_type: str
    answer_value: Optional[str]


class TokenFormSnapshotRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    token_id: int
    form_id: Optional[int]
    form_title: str
    form_version: int
    answers: list[TokenFormAnswerRead] = []
    created_at: datetime
