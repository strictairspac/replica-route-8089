from pydantic import BaseModel,Field
class WindowInput(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    default_hold_ms:int=Field(default=100,ge=0,le=86400000)
    horizon_ms:int=Field(ge=1,le=86400000)
class WindowTrialInput(BaseModel):
    """向共享窗口发起一次试验。读事件的 hold_ms/latest_start_ms 在请求集事件中配置。"""
    name:str=Field(min_length=2,max_length=120)
    request_set_id:int=Field(gt=0)
    max_delay_ms:int=Field(default=100,ge=0,le=3600000)
    sticky_ms:int=Field(default=500,ge=0,le=3600000)
    cancel_at_ms:int|None=Field(default=None,ge=0,le=86400000)
class CancelInput(BaseModel):
    cancel_at_ms:int=Field(ge=0,le=86400000)
