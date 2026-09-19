"""Reload the research context pipeline on fixed validation articles; no test metrics."""
import argparse
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import torch

from news_company_context import ContextEncoder
from news_content import sanitize_article
from news_impact_data import node_features, graph_arrays, mention_array
from news_impact_model import NewsGAT
from verify_news_predictive_value import dump,sha

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/"ner"))
from matcher import CompanyMatcher


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--experiment",required=True,type=Path);args=ap.parse_args()
    out=args.experiment.resolve();snapshot=out/"training_inputs"
    completion=json.loads((out/"completion.json").read_text(encoding="utf8"))
    assert completion["validation_only"]
    protocol=json.loads((out/"protocol.json").read_text(encoding="utf8"))
    for path,digest in protocol["source_hashes"].items():assert sha(Path(path))==digest,path
    for name,digest in protocol["code_hashes"].items():assert sha(HERE/name)==digest,name
    docs=[json.loads(l) for l in (snapshot/"articles.jsonl").open(encoding="utf8")]
    pairs=pd.read_parquet(snapshot/"pairs.parquet")
    ids=[]
    for region in ("domestic","overseas"):
        eligible=[i for i,d in enumerate(docs) if d["split"]=="val" and d["region"]==region]
        ids += [eligible[0],eligible[len(eligible)//2],eligible[-1]]
    selected=[sanitize_article(docs[i]) for i in ids]
    matcher=CompanyMatcher.from_csv(snapshot/"aliases.csv");hits=[]
    for d in selected:
        direct={m["ticker"] for m in d["companies"]}
        hits.append([{"ticker":m.ticker,"start":m.start,"end":m.end}
            for m in matcher.match(d.get("title"),d.get("body")).mentions if m.ticker in direct and m.confidence>=1.0])
    torch.set_num_threads(4);encoder=ContextEncoder(snapshot,device="cpu")
    embedding,_=encoder.encode_role("embedding",selected,hits);encoder.release()
    sentiment=np.zeros((len(ids),3),dtype="float32")
    for role in ("sentiment_ko","sentiment_en"):
        values,_=encoder.encode_role(role,selected,hits);sentiment+=values;encoder.release()
    stored_embedding=np.load(snapshot/"embeddings.npy")[ids]
    stored_sentiment=np.load(snapshot/"sentiment.npy")[ids]
    np.testing.assert_allclose(embedding,stored_embedding,atol=2e-5,rtol=1e-4)
    np.testing.assert_allclose(sentiment,stored_sentiment,atol=2e-5,rtol=1e-4)
    with np.load(snapshot/"text_projection.npz") as projection:
        text=((embedding-projection["mean"])@projection["components"].T).astype("float32")
    meta=pd.read_json(snapshot/"companies.json",dtype={"ticker":str});tickers=meta.ticker.tolist()
    graphs=json.loads((snapshot/"graphs.json").read_text(encoding="utf8"))
    models=[]
    for seed in range(3):
        checkpoint=torch.load(out/f"gat_seed{seed}.pt",map_location="cpu",weights_only=True)
        model=NewsGAT(**checkpoint["config"]).eval();model.load_state_dict(checkpoint["state_dict"]);models.append(model)
    with np.load(out/"predictions.npz") as stored:
        expected_p=stored["probabilities"];expected_m=stored["magnitude"]
    differences=[]
    with torch.inference_mode():
        for j,i in enumerate(ids):
            d=docs[i];g=graphs[d["graph"]];e,a=graph_arrays(g,tickers)
            inputs=[torch.from_numpy(x) for x in (text[j:j+1],sentiment[j:j+1],
                mention_array(d["companies"],tickers)[None],node_features(meta,g),e,a)]
            outputs=[model(*inputs) for model in models]
            p=torch.stack([x.softmax(-1) for x,m in outputs]).mean(0)[0].numpy()
            m=torch.stack([m for x,m in outputs]).mean(0)[0].numpy()
            rows=np.flatnonzero(pairs.article.to_numpy()==i)
            ni=np.array([tickers.index(t) for t in pairs.iloc[rows].ticker])
            np.testing.assert_allclose(p[ni],expected_p[rows],atol=2e-5,rtol=1e-4)
            np.testing.assert_allclose(m[ni],expected_m[rows],atol=2e-5,rtol=1e-4)
            differences.append(max(float(abs(p[ni]-expected_p[rows]).max()),float(abs(m[ni]-expected_m[rows]).max())))
    result={"articles":ids,"validation_only":True,"source_unchanged":True,
        "max_embedding_error":float(abs(embedding-stored_embedding).max()),
        "max_sentiment_error":float(abs(sentiment-stored_sentiment).max()),"max_prediction_error":max(differences)}
    dump(out/"reload_verification.json",result);print(json.dumps(result),flush=True)


if __name__=="__main__":main()
