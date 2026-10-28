"""
models/custom_form.py
─────────────────────
Dynamic custom form builder, fields, options, conditional logic,
and immutable submission snapshots for token bookings.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class CustomForm(Base, TimestampMixin):
    __tablename__ = "custom_forms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="custom_forms")
    service: Mapped["Service | None"] = relationship("Service", back_populates="custom_forms")
    fields: Mapped[list["FormField"]] = relationship(
        "FormField", back_populates="form", cascade="all, delete-orphan", order_by="FormField.order_index"
    )

    def __repr__(self) -> str:
        return f"<CustomForm id={self.id} title={self.title!r} v={self.version} published={self.is_published}>"


class FormField(Base, TimestampMixin):
    __tablename__ = "form_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    form_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("custom_forms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    field_type: Mapped[str] = mapped_column(String(50), nullable=False)  # text, number, select, radio, checkbox, textarea, date, file
    placeholder: Mapped[str | None] = mapped_column(String(200), nullable=True)
    help_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    validation_regex: Mapped[str | None] = mapped_column(String(255), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    form: Mapped["CustomForm"] = relationship("CustomForm", back_populates="fields")
    options: Mapped[list["FormFieldOption"]] = relationship(
        "FormFieldOption", back_populates="field", cascade="all, delete-orphan", order_by="FormFieldOption.order_index"
    )
    conditions: Mapped[list["FormFieldCondition"]] = relationship(
        "FormFieldCondition",
        foreign_keys="FormFieldCondition.field_id",
        back_populates="field",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<FormField id={self.id} key={self.field_key!r} type={self.field_type}>"


class FormFieldOption(Base):
    __tablename__ = "form_field_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    field_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("form_fields.id", ondelete="CASCADE"), nullable=False, index=True
    )
    option_label: Mapped[str] = mapped_column(String(200), nullable=False)
    option_value: Mapped[str] = mapped_column(String(200), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    field: Mapped["FormField"] = relationship("FormField", back_populates="options")

    def __repr__(self) -> str:
        return f"<FormFieldOption id={self.id} label={self.option_label!r} value={self.option_value!r}>"


class FormFieldCondition(Base):
    __tablename__ = "form_field_conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    field_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("form_fields.id", ondelete="CASCADE"), nullable=False, index=True
    )
    depends_on_field_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("form_fields.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operator: Mapped[str] = mapped_column(String(50), nullable=False)  # equals, not_equals, contains, greater_than, less_than
    trigger_value: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(50), default="show", nullable=False)  # show, hide, require

    # Relationships
    field: Mapped["FormField"] = relationship(
        "FormField", foreign_keys=[field_id], back_populates="conditions"
    )
    depends_on_field: Mapped["FormField"] = relationship(
        "FormField", foreign_keys=[depends_on_field_id]
    )


class TokenFormSnapshot(Base, TimestampMixin):
    __tablename__ = "token_form_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    form_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("custom_forms.id", ondelete="SET NULL"), nullable=True, index=True
    )
    form_title: Mapped[str] = mapped_column(String(200), nullable=False)
    form_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    schema_snapshot: Mapped[str] = mapped_column(Text, nullable=False)  # JSON representation of form definition

    # Relationships
    token: Mapped["Token"] = relationship("Token", back_populates="form_snapshot")
    form: Mapped["CustomForm | None"] = relationship("CustomForm")
    answers: Mapped[list["TokenFormAnswer"]] = relationship(
        "TokenFormAnswer", back_populates="snapshot", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<TokenFormSnapshot token={self.token_id} form={self.form_title!r} v={self.form_version}>"


class TokenFormAnswer(Base, TimestampMixin):
    __tablename__ = "token_form_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("token_form_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    field_label: Mapped[str] = mapped_column(String(200), nullable=False)
    field_type: Mapped[str] = mapped_column(String(50), nullable=False)
    answer_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    snapshot: Mapped["TokenFormSnapshot"] = relationship("TokenFormSnapshot", back_populates="answers")

    def __repr__(self) -> str:
        return f"<TokenFormAnswer key={self.field_key!r}>"
