from sqlalchemy import Column, Integer, String, JSON, Text, Float, UniqueConstraint
from src.config.database import Base


class HundredEdition(Base):
    """百件版：由一份已复核报告生成的整版，按日期×试制线×班次分格。"""
    __tablename__ = 'hundred_editions'
    __table_args__ = (
        UniqueConstraint('period', 'policy_version', 'version_no', name='uq_hundred_version'),
    )
    id = Column(Integer, primary_key=True)
    request_key = Column(String(120), unique=True, nullable=False)
    run_id = Column(Integer, nullable=False)
    run_checksum = Column(String(64))
    period = Column(String(120), nullable=False)
    policy_version = Column(String(120), nullable=False)
    base_edition_id = Column(Integer)
    base_version_no = Column(Integer)
    status = Column(String(20), nullable=False)  # queued/running/published/rejected/failed
    version_no = Column(Integer)
    reason = Column(Text, default='')
    result = Column(JSON)
    checksum = Column(String(64))
    changes = Column(Integer)
    deviation = Column(Integer)
    created = Column(Float)
    finished = Column(Float)


class HundredSlot(Base):
    """同一期间×口径的发布槽位，用于并发时判定上版是否已换。"""
    __tablename__ = 'hundred_slots'
    __table_args__ = (
        UniqueConstraint('period', 'policy_version', name='uq_hundred_slot'),
    )
    id = Column(Integer, primary_key=True)
    period = Column(String(120), nullable=False)
    policy_version = Column(String(120), nullable=False)
    latest_edition_id = Column(Integer)
    next_version = Column(Integer, nullable=False, default=1)
