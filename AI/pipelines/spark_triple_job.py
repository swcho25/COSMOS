"""PySpark batch job that deduplicates triples and calculates edge confidence."""

from __future__ import annotations

from pyspark.sql import SparkSession, functions as F, types as T


TRIPLE_SCHEMA = T.StructType(
    [
        T.StructField("source", T.StringType(), False),
        T.StructField("relation", T.StringType(), False),
        T.StructField("target", T.StringType(), False),
        T.StructField("weight", T.DoubleType(), True),
        T.StructField("confidence", T.DoubleType(), True),
        T.StructField("evidence", T.StringType(), True),
        T.StructField("observed_at", T.TimestampType(), True),
    ]
)


def run(input_path: str, output_path: str) -> None:
    spark = SparkSession.builder.appName("nexus-risk-triple-etl").getOrCreate()
    raw = spark.read.schema(TRIPLE_SCHEMA).json(input_path)
    normalized = (
        raw.filter(F.col("source").isNotNull() & F.col("target").isNotNull())
        .withColumn("weight", F.greatest(F.lit(0.0), F.least(F.lit(1.0), F.coalesce("weight", F.lit(0.0)))))
        .withColumn("confidence", F.coalesce("confidence", F.when(F.col("evidence").contains("DART"), .95).otherwise(.65)))
        .withColumn("edge_key", F.sha2(F.concat_ws("|", "source", "relation", "target"), 256))
    )
    latest = normalized.groupBy("edge_key").agg(
        F.max_by(F.struct("source", "relation", "target", "weight", "confidence", "evidence", "observed_at"), "observed_at").alias("row")
    )
    latest.select("row.*").write.mode("overwrite").parquet(output_path)
    spark.stop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(args.input, args.output)
