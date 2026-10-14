from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')
    @field_validator('*',mode='before')
    @classmethod
    def trim_non_secret(cls,value,info):
        return value.strip() if isinstance(value,str) and info.field_name!='password' else value
class EquipmentIn(Strict):
    code:str=Field(min_length=1,max_length=80)
    type:Literal['manifold','extinguisher','hydrant']
    subtype:Literal['indoor','outdoor']|None=None
    name:str=Field(min_length=1,max_length=120)
    location_text:str=Field(min_length=1,max_length=300)
    layout_id:int=Field(default=1,ge=1)
    x:float=Field(ge=0,le=1,allow_inf_nan=False)
    y:float=Field(ge=0,le=1,allow_inf_nan=False)
    @model_validator(mode='after')
    def subtype_required(self):
        if self.type=='hydrant' and self.subtype is None:raise ValueError('Hydrant subtype is required')
        if self.type!='hydrant':self.subtype=None
        return self
class EquipmentUpdate(EquipmentIn):version:int=Field(ge=1)
class Version(Strict):version:int=Field(ge=1)
class Delete(Version):reason:str=Field(min_length=1,max_length=1000)
class Assignment(Strict):
    assignee_id:int|None=Field(default=None,ge=1)
    version:int=Field(default=0,ge=0)
class UserIn(Strict):
    username:str=Field(min_length=1,max_length=100)
    display_name:str=Field(default='',max_length=100)
    password:str=Field(min_length=12,max_length=256)
    role:Literal['admin','inspector','viewer']
class UserUpdate(Version):
    display_name:str=Field(default='',max_length=100)
    role:Literal['owner','admin','inspector','viewer']
    active:bool
    password:str|None=Field(default=None,min_length=12,max_length=256)
class Item(Strict):
    item_code:str=Field(min_length=1,max_length=30)
    result:Literal['good','deficient','na']|None=None
    comment:str=Field(default='',max_length=4000)
    not_applicable_reason:str=Field(default='',max_length=2000)
class InspectionIn(Strict):
    version:int=Field(default=0,ge=0)
    submit:bool=False
    request_id:str=Field(min_length=16,max_length=100)
    items:list[Item]=Field(max_length=30)
class FindingAction(Version):
    action:Literal['resolve','reopen']
    year:int=Field(ge=2000,le=2100)
    quarter:int=Field(ge=1,le=4)
    comment:str=Field(min_length=1,max_length=4000)
class Restore(Strict):
    confirmation:Literal['RESTORE']
