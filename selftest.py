import time, websearch as W
qs=["泉州 2027届 校园招聘 质量工程师","DeepSeek V4.1 Flash 定价","泉州 到 厦门 高铁 时刻表",
    "简历 自我评价 质量工程师 怎么写","2026年 国庆 放假 安排","福建 高温 预警 今天"]
ok=0
for q in qs:
    t=time.time(); r=W.search(q, limit=6)
    dt=int((time.time()-t)*1000); n=len(r["results"]); rel=r.get("relevance") or 0
    good=n>=2 and rel>=0.34; ok+=1 if good else 0
    print(f"{'✓' if good else '✗'} {q[:24]:<26} [{r['engine']} {dt}ms n={n} rel={rel}]")
    if n: print("    1)",r['results'][0]['title'][:56])
    else: print("    ", (r['problems'][-1] if r['problems'] else '')[:100])
    time.sleep(8)
print(f"\n可用 {ok}/{len(qs)}")
