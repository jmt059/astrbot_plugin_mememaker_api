from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from datetime import datetime

class MemeOption(BaseModel):
    name: str
    type: str
    default: Optional[Any] = None
    description: Optional[str] = None
    choices: Optional[List[Any]] = None
    minimum: Optional[Any] = None
    maximum: Optional[Any] = None
    parser_flags: Dict[str, Any] = Field(default_factory=dict)

class MemeParams(BaseModel):
    min_images: int
    max_images: int
    min_texts: int
    max_texts: int
    # 【修正】使用 Field(default_factory=list) 作为列表的默认值，更安全
    default_texts: List[str] = Field(default_factory=list)
    options: List[MemeOption] = Field(default_factory=list)

class MemeInfo(BaseModel):
    key: str
    params: MemeParams
    keywords: List[str] = Field(default_factory=list)
    shortcuts: List[Dict] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    # 【修正】统一使用 datetime 类型，Pydantic会自动转换API返回的日期字符串
    date_created: datetime