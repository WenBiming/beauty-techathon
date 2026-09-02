import os, json, time, collections, sys
from dotenv import load_dotenv; load_dotenv("/Users/wenbiming/dev/beauty-techathon/.env")
from openai import OpenAI
import openpyxl

XLSX="/Users/wenbiming/Documents/misc/AI-Assist/赛题 1：数据共情者-业务数据.xlsx"
wb=openpyxl.load_workbook(XLSX, data_only=True)
rows=list(wb["聊天记录"].iter_rows(values_only=True)); h=rows[0]
chat=[dict(zip(h,r)) for r in rows[1:] if r[0] is not None]
MAJOR=sorted({r["scene_major"] for r in chat}); MINOR=sorted({r["scene_minor"] for r in chat})
sess=collections.OrderedDict()
for r in chat: sess.setdefault(r["会话ID"],[]).append(r)

# 测试集：魏h** 的 3 个会话 + 覆盖各 major 的 7 个会话
pick=["S00005","S00059","S00099"]
seen={sess[s][0]["scene_major"] for s in pick}
for sid,ms in sess.items():
    if sid in pick: continue
    if ms[0]["scene_major"] not in seen:
        pick.append(sid); seen.add(ms[0]["scene_major"])
    if len(pick)>=12: break

SYS=f"""你是美妆电商客服辅助系统的会话分析引擎。给定一段完整的客服-买家对话，输出结构化分析。

scene_major 必须严格从以下列表选一个：{MAJOR}
scene_minor 必须严格从以下列表选一个：{MINOR}

只输出 JSON，不要 markdown 代码块，不要解释。格式：
{{"scene_major":"...","scene_minor":"...","confidence":0.0-1.0,"emotion":1-5,"summary":"一句话不超过30字","risks":["风险标签"],"promises":[{{"text":"客服承诺原文","deadline_hours":数字或null}}]}}
emotion: 1=极度不满 2=不满 3=中性 4=满意 5=非常满意"""

c=OpenAI(api_key=os.environ["DASHSCOPE_API_KEY"], base_url=os.environ["DASHSCOPE_BASE_URL"])
MODEL=sys.argv[1] if len(sys.argv)>1 else "qwen3.8-flash"
ok=maj=mino=0; tin=tout=0; t0=time.time()
print(f"模型: {MODEL}   测试会话: {len(pick)}\n")
for sid in pick:
    ms=sess[sid]
    dlg="\n".join(f"{m['角色']}: {m['message_text']}" for m in ms)
    try:
        r=c.chat.completions.create(model=MODEL, messages=[{"role":"system","content":SYS},{"role":"user","content":dlg}],
                                    temperature=0.1, max_tokens=800, extra_body={"enable_thinking":False})
        tin+=r.usage.prompt_tokens; tout+=r.usage.completion_tokens
        txt=r.choices[0].message.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        d=json.loads(txt); ok+=1
    except Exception as e:
        print(f"{sid} ✗ {type(e).__name__}: {str(e)[:150]}"); continue
    gM,gm=ms[0]["scene_major"],ms[0]["scene_minor"]
    m1=d.get("scene_major")==gM; m2=d.get("scene_minor")==gm
    maj+=m1; mino+=m2
    print(f"{sid} major {'✓' if m1 else '✗ '+str(d.get('scene_major'))}/{gM}  minor {'✓' if m2 else '✗ '+str(d.get('scene_minor'))}/{gm}")
    print(f"      情绪{d.get('emotion')} | {d.get('summary')}")
    print(f"      风险{d.get('risks')} | 承诺{len(d.get('promises',[]))}条 {[p.get('text','')[:22] for p in d.get('promises',[])]}")
print(f"\n{'='*60}\nJSON解析成功 {ok}/{len(pick)} | major准确 {maj}/{len(pick)} | minor准确 {mino}/{len(pick)}")
print(f"token: in={tin} out={tout} 合计={tin+tout} | 单会话均值={(tin+tout)//max(ok,1)} | 耗时{time.time()-t0:.1f}s")
