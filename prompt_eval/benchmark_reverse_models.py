#!/usr/bin/env python3
"""Read-only live API benchmark for reverse questions. No app/UI writes or fallback.

Test artifacts (including frozen inputs) go only into /tmp, never the app/repo.
Use --unpack PACK.json to inspect an exported pack. No reasoning text is retained.
"""
from __future__ import annotations
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'lanshot2')]
from lanshot2.audio_service import BAILIAN_URL, load_api_key, load_google_api_key, google_request_opener
from lanshot2.reverse_questions import REVERSE_QUESTION_SYSTEM_PROMPT

SOURCE_ROOT = Path('/Users/zhouguichao/Desktop/面试分析/面试记录')
CASES = {
 'E0918': {'path':'2026-09-18_某AI教育大模型团队_Agent与评测实习/原始会话.txt','format':'lanshot','before_turn':11,'notes':'第11段含实际反问及答案，整段排除。前10段是采集批次，不是精确问答轮次。'},
 'T0918': {'path':'2026-09-18_某数字化业务大厂_业务开发TL面/原始会话.txt','format':'lanshot','before_turn':7,'notes':'第7段含实际反问及后续反馈，整段排除。按原文批次顺序，不按提交时间重排。'},
 'G0916': {'path':'2026-09-16_广州大模型团队_Agent开发实习/原始会话.txt','format':'lanshot','before_turn':10,'notes':'第10段开始出现反问邀请，保守排除第10、11段。后续团队业务不提前给模型。'},
 'A0826': {'path':'2026-08-26_Agent实习/转写.md','format':'diarized','notes':'只保留带时间戳的原始转写，原文20:45后只有纪要而无完整逐句转写，不把纪要补入。此样本是部分会话。','roles':{'说话人 A':'interviewer','说话人 B':'candidate'}},
 'V0902': {'path':'2026-09-02_达人营销_AI员工方向/转写.txt','format':'diarized','before_time':'24:20','notes':'在24:20反问邀请前截断。B为候选人，E为主要面试官，其余说话人保留unknown。','roles':{'说话人 E':'interviewer','说话人 B':'candidate'}},
 'P0907': {'path':'2026-09-07_政企大模型应用开发/转写.txt','format':'diarized','before_time':'53:57','notes':'原始逐句记录几乎没有面试官完整语句；不得用后附纪要重建其追问或观点。这是缺失说话人压力样本，不是完整双路正例。','roles':{'周贵超':'candidate'}},
}
VARIANTS = {
 'ds_flash_off': {'provider':'bailian','model':'deepseek-v4.1-flash','enable_thinking':False},
 'ds_flash_high': {'provider':'bailian','model':'deepseek-v4.1-flash','enable_thinking':True,'reasoning_effort':'high'},
 'ds_pro_high': {'provider':'bailian','model':'deepseek-v4-pro-0813','enable_thinking':True,'reasoning_effort':'high'},
 'qwen_think': {'provider':'bailian','model':'qwen3.8-max-0902','enable_thinking':True,'thinking_budget':4096},
 'glm_think': {'provider':'bailian','model':'glm-5.3','enable_thinking':True,'thinking_budget':4096},
 'glm_low': {'provider':'bailian','model':'glm-5.3','enable_thinking':True,'reasoning_effort':'low'},
 'glm_high': {'provider':'bailian','model':'glm-5.3','enable_thinking':True,'reasoning_effort':'high'},
 'gemini_low': {'provider':'google','model':'gemini-3.8-flash','thinking_level':'LOW'},
 'gemini_high': {'provider':'google','model':'gemini-3.8-flash','thinking_level':'HIGH'},
}

def digest(value: str) -> str:
 return hashlib.sha256(value.encode('utf-8')).hexdigest()

def prepare(case_id: str) -> dict:
 spec = CASES[case_id]
 text = (SOURCE_ROOT / spec['path']).read_text(encoding='utf-8')
 evidence = {}
 output = []
 if spec['format'] == 'lanshot':
  active = False
  speaker = 'unknown'
  for n, line in enumerate(text.splitlines(), 1):
   match = re.match(r'【第\s*(\d+)\s*题', line)
   if match:
    if int(match[1]) >= spec['before_turn']:
     break
    active = False
    output.append('【采集批次 '+match[1]+'；两路文字各自有序，跨路先后不确定】')
   if line.strip() == '--- 问题输入 ---':
    active = True
    continue
   if line.strip() == '--- 模型回答 ---':
    active = False
    continue
   if not active:
    continue
   if line.strip() in ('系统声音识别：','麦克风识别：'):
    speaker = 'interviewer' if line.startswith('系统') else 'candidate'
    output.append('【'+speaker+'】')
    continue
   if line.strip():
    evidence[str(n)] = {'text':line,'speaker':speaker}
    output.append(f'[L{n}] {line}')
 else:
  for n, line in enumerate(text.splitlines(), 1):
   match = re.match(r'\[(\d\d:\d\d)\]\s*([^：]+)：(.*)',line)
   if not match:
    continue
   if spec.get('before_time') and match[1] >= spec['before_time']:
    break
   speaker = spec.get('roles',{}).get(match[2], 'unknown')
   # Preserve source words; only role metadata is added.
   evidence[str(n)] = {'text':line,'speaker':speaker}
   output.append(f'[L{n}] [{speaker}] {line}')
 dialogue = '\n'.join(output)
 if not evidence or '--- 模型回答 ---' in dialogue:
  raise ValueError('invalid prepared input: '+case_id)
 return {'case_id':case_id,'source':spec['path'],'source_sha256':digest(text),
         'input_sha256':digest(dialogue),'notes':spec['notes'],
         'dialogue':dialogue,'evidence':evidence,'input_chars':len(dialogue)}

def inspect_answer(answer: str, item: dict, current: bool) -> dict:
 result = {'strict_json':False,'count':0,'evidence_total':0,'evidence_valid':0,
           'schema_valid':False,'issues':[],'questions':[]}
 try:
  rows = json.loads(answer)
 except json.JSONDecodeError:
  result['issues'].append('not_strict_json')
  return result
 result['strict_json'] = True
 if not isinstance(rows,list):
  result['issues'].append('not_array')
  return result
 result['count'] = len(rows)
 valid = len(rows) <= 3
 for row in rows:
  required = ('priority','category','topic','question','context_reason','score')
  if not isinstance(row,dict) or not all(k in row for k in required):
   valid = False
   continue
  if row['priority'] not in ('P0','P1','P2','P3') or not isinstance(row['question'],str):
   valid = False
   continue
  q = row['question']
  result['questions'].append(q)
  if len(q) > 120:
   result['issues'].append('long_question')
  if any(x in q for x in ('我答得','我刚才没','回答得不够','回答得比较','贵团队','贵司','反直觉')):
   result['issues'].append('template_or_self_deprecation')
  evs = row.get('evidence',[])
  if not current and not evs:
   valid = False
   result['issues'].append('missing_evidence')
  for ev in evs:
   result['evidence_total'] += 1
   src = item['evidence'].get(str(ev.get('line')),{}) if isinstance(ev,dict) else {}
   quote = ev.get('quote','') if isinstance(ev,dict) else ''
   if isinstance(quote,str) and len(quote)>=2 and quote in src.get('text','') and ev.get('speaker') == src.get('speaker'):
    result['evidence_valid'] += 1
   else:
    result['issues'].append('evidence_mismatch')
 result['schema_valid'] = valid
 return result

def run_one(variant_id: str, item: dict, prompt: str, current: bool) -> dict:
 variant = VARIANTS[variant_id]
 provider = variant['provider']
 user = '记录边界说明：'+item['notes']+'\n以下为此刻可见记录，原始行号用于引用：\n'+item['dialogue']
 if provider == 'bailian':
  payload = {'model':variant['model'],'messages':[{'role':'system','content':prompt},{'role':'user','content':user}],
             'stream':True,'stream_options':{'include_usage':True},'max_tokens':16384,'temperature':0.2}
  payload.update({k:v for k,v in variant.items() if k in ('enable_thinking','reasoning_effort','thinking_budget')})
  request = urllib.request.Request(BAILIAN_URL,data=json.dumps(payload,ensure_ascii=False).encode(),
          headers={'Authorization':'Bearer '+load_api_key(),'Content-Type':'application/json'},method='POST')
  opener = urllib.request.urlopen
 else:
  payload = {'systemInstruction':{'parts':[{'text':prompt}]},'contents':[{'role':'user','parts':[{'text':user}]}],
             'generationConfig':{'temperature':0.2,'maxOutputTokens':12288,'thinkingConfig':{'thinkingLevel':variant['thinking_level']}}}
  url = 'https://generativelanguage.googleapis.com/v1beta/models/'+variant['model']+':streamGenerateContent?alt=sse'
  request = urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),
          headers={'x-goog-api-key':load_google_api_key(),'Content-Type':'application/json'},method='POST')
  opener = google_request_opener()
 started = time.monotonic()
 answer = ''
 usage = {}
 finish = []
 actual_model = ''
 request_id = ''
 first_ms = None
 reasoning_chars = 0
 status = 'ok'
 error = ''
 try:
  with opener(request,timeout=150) as response:
   for raw in response:
    if time.monotonic()-started > 210:
     raise TimeoutError('total time budget exceeded')
    line = raw.decode('utf-8',errors='replace').strip()
    if not line.startswith('data:'):
     continue
    data = line[5:].strip()
    if not data or data == '[DONE]':
     continue
    event = json.loads(data)
    if event.get('error'):
     raise RuntimeError('provider stream error')
    if provider == 'bailian':
     if event.get('usage'):
      usage = event['usage']
     actual_model = event.get('model',actual_model)
     request_id = event.get('id',request_id)
     for choice in event.get('choices',[]):
      delta = choice.get('delta',{})
      reasoning_chars += len(delta.get('reasoning_content') or '')
      value = delta.get('content') or ''
      if value:
       if first_ms is None:
        first_ms = round((time.monotonic()-started)*1000)
       answer += value
      if choice.get('finish_reason'):
       finish.append(choice['finish_reason'])
    else:
     if event.get('usageMetadata'):
      usage = event['usageMetadata']
     actual_model = event.get('modelVersion',actual_model)
     request_id = event.get('responseId',request_id)
     for choice in event.get('candidates',[]):
      for part in choice.get('content',{}).get('parts',[]):
       value = part.get('text','')
       if part.get('thought'):
        reasoning_chars += len(value)
       else:
        if value and first_ms is None:
         first_ms = round((time.monotonic()-started)*1000)
        answer += value
      if choice.get('finishReason'):
       finish.append(choice['finishReason'])
  if any(v in ('length','MAX_TOKENS') for v in finish):
   status='truncated'
   error='provider output budget exhausted'
  if not answer:
   raise RuntimeError('empty final answer')
 except urllib.error.HTTPError as exc:
  status='error'
  error='HTTP '+str(exc.code)
  exc.close()
 except Exception as exc:
  status='error'
  error=type(exc).__name__+': '+str(exc)[:160]
 return {'case_id':item['case_id'],'variant':variant_id,'requested':variant,'actual_model':actual_model,
         'request_id':request_id,'status':status,'error':error,'prompt_sha256':digest(prompt),
         'input_sha256':item['input_sha256'],'generation_limits':{'max_tokens':16384 if provider=='bailian' else 12288,'timeout_seconds':150,'temperature':0.2},'elapsed_ms':round((time.monotonic()-started)*1000),
         'first_visible_ms':first_ms,'reasoning_chars_observed':reasoning_chars,'usage':usage,
         'finish_reasons':finish,'answer':answer,'checks':inspect_answer(answer,item,current)}

def main() -> int:
 ap=argparse.ArgumentParser()
 ap.add_argument('--ids',default='E0918,T0918,G0916')
 ap.add_argument('--variants',default='ds_flash_off,ds_flash_high,ds_pro_high,qwen_think,glm_low,gemini_low,gemini_high')
 ap.add_argument('--prompt',choices=('current','v2','v3'),default='v3')
 ap.add_argument('--workers',type=int,default=4)
 ap.add_argument('--prepare-only',action='store_true')
 ap.add_argument('--unpack',type=Path)
 args=ap.parse_args()
 if args.unpack:
  pack=json.loads(args.unpack.read_text())
  print(gzip.decompress(base64.b64decode(pack['gzip_base64'])).decode())
  return 0
 items=[prepare(k) for k in args.ids.split(',')]
 if args.prepare_only:
  for item in items:
   print(json.dumps({k:v for k,v in item.items() if k not in ('dialogue','evidence')},ensure_ascii=False))
  return 0
 prompt=REVERSE_QUESTION_SYSTEM_PROMPT if args.prompt=='current' else (ROOT/('prompt_eval/reverse_question_prompt_'+args.prompt+'.txt')).read_text()
 results=[]
 with ThreadPoolExecutor(max_workers=min(max(args.workers,1),7)) as pool:
  jobs={pool.submit(run_one,v,item,prompt,args.prompt=='current'):(item['case_id'],v) for item in items for v in args.variants.split(',')}
  for future in as_completed(jobs):
   row=future.result()
   results.append(row)
   print(json.dumps({'type':'result','case_id':row['case_id'],'variant':row['variant'],'status':row['status'],
         'elapsed_ms':row['elapsed_ms'],'usage':row['usage'],'finish_reasons':row['finish_reasons'],
         'error':row['error'],'checks':row['checks']},ensure_ascii=False),flush=True)
 record={'created_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'prompt_version':args.prompt,'prompt':prompt,'inputs':items,'results':results}
 artifact_dir=Path('/tmp/LanShotReverseEval_20260920')
 artifact_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
 artifact=artifact_dir/(args.prompt+'_'+'-'.join(args.ids.split(','))+'_'+str(time.time_ns())+'.json')
 artifact.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
 artifact.chmod(0o600)
 print(json.dumps({'type':'archive','path':str(artifact),'result_count':len(results)},ensure_ascii=False),flush=True)
 return 0

if __name__=='__main__':
 raise SystemExit(main())
