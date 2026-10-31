from server_emulator.runtime import Runtime

async def test_fixed_reply_never_calls_ai_or_tools(tmp_path):
    runtime=Runtime(tmp_path)
    runtime.settings['model']['stub_text']='Проверка связи. Я вас слышу.'
    async def forbidden(*args,**kwargs):raise AssertionError('AI must not be called')
    runtime.provider=forbidden
    try:
        for text in ['привет','поставь таймер','расскажи шутку']:
            result=await runtime.chat(text,history=[{'role':'user','content':'предыдущая фраза'}])
            assert result['text']=='Проверка связи. Я вас слышу.'
            assert result['source']=='stub' and result['tool_calls']==[]
        assert runtime.pending_calls==[]
    finally:await runtime.close()

async def test_empty_stub_keeps_original_provider(tmp_path):
    import json
    runtime=Runtime(tmp_path)
    async def provider(*args,**kwargs):
        return json.dumps({'choices':[{'message':{'content':'Модель'}}]}).encode(),'application/json'
    runtime.provider=provider
    runtime.settings['model']['stub_text']=''
    try:assert (await runtime.chat('привет'))['text']=='Модель'
    finally:await runtime.close()
