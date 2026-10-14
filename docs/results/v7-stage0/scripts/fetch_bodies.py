import json,urllib.request,time,os,sys
os.chdir(sys.argv[1])
idx=json.load(open("catalog161_index.json"))["articles"]; W=1751328000000; n=0
for a in idx:
    if a["releaseDate"]>=W or os.path.exists(f"bodies/{a['code']}.json"): continue
    u=f"https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?articleCode={a['code']}"
    for k in range(8):
        try:
            b=urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=30).read(); break
        except Exception as e:
            time.sleep(30*(k+1))
    else:
        print("fail",a["code"],flush=True); continue
    open(f"bodies/{a['code']}.json","wb").write(b); n+=1; time.sleep(2.5)
print("done fetched",n,flush=True)
