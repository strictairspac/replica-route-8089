from fastapi import APIRouter,Depends
from fastapi.responses import Response
import json
from src.config.database import db_session
from src.utils.security import current,writer
from src.schemas.window import WindowInput,WindowTrialInput,CancelInput
from src.services import windows as service
router=APIRouter()
@router.get('/windows')
def listing(db=Depends(db_session),user=Depends(current)):return service.listing(db)
@router.post('/windows',status_code=201)
def create(data:WindowInput,db=Depends(db_session),user=Depends(writer)):return service.create(db,data,user)
@router.get('/windows/{ident}')
def detail(ident:int,db=Depends(db_session),user=Depends(current)):return service.detail(db,ident)
@router.post('/windows/{ident}/trials',status_code=201)
def add_trial(ident:int,data:WindowTrialInput,db=Depends(db_session),user=Depends(writer)):
    return service.add_trial(db,ident,data,user)
@router.post('/windows/{ident}/trials/{trial_id}/cancel')
def cancel(ident:int,trial_id:int,data:CancelInput,db=Depends(db_session),user=Depends(writer)):
    return service.cancel_trial(db,ident,trial_id,data,user)
@router.post('/windows/{ident}/execute')
def execute(ident:int,db=Depends(db_session),user=Depends(writer)):return service.execute(db,ident,user)
@router.get('/windows/{ident}/export')
def export(ident:int,db=Depends(db_session),user=Depends(current)):
    return Response(json.dumps(service.detail(db,ident),ensure_ascii=False,indent=2),
                    media_type='application/json',
                    headers={'Content-Disposition':f'attachment; filename="window-{ident}.json"'})
