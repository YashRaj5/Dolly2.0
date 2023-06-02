# Databricks notebook source
# MAGIC %md-sandbox
# MAGIC # Dolly: data Preparation & Vector database creation with Databricks Lakehouse
# MAGIC
# MAGIC <img style="float: right" width="600px" src="https://raw.githubusercontent.com/databricks-demos/dbdemos-resources/main/images/product/llm-dolly/llm-dolly-data-prep-small.png">
# MAGIC
# MAGIC To be able to specialize our mode, we need a list of Q&A that we'll use as training dataset.
# MAGIC
# MAGIC For this demo, we'll specialize our model using Stack Exchange dataset. 
# MAGIC
# MAGIC Let's start with a simple data pipeline ingesting the Stack Exchange dataset, running some cleanup & saving it for further training.
# MAGIC
# MAGIC We will implement the following steps: <br><br>
# MAGIC
# MAGIC <style>
# MAGIC .right_box{
# MAGIC   margin: 30px; box-shadow: 10px -10px #CCC; width:650px; height:300px; background-color: #1b3139ff; box-shadow:  0 0 10px  rgba(0,0,0,0.6);
# MAGIC   border-radius:25px;font-size: 35px; float: left; padding: 20px; color: #f9f7f4; }
# MAGIC .badge {
# MAGIC   clear: left; float: left; height: 30px; width: 30px;  display: table-cell; vertical-align: middle; border-radius: 50%; background: #fcba33ff; text-align: center; color: white; margin-right: 10px; margin-left: -35px;}
# MAGIC .badge_b { 
# MAGIC   margin-left: 25px; min-height: 32px;}
# MAGIC </style>
# MAGIC
# MAGIC
# MAGIC <div style="margin-left: 20px">
# MAGIC   <div class="badge_b"><div class="badge">1</div> Download raw Q&A dataset</div>
# MAGIC   <div class="badge_b"><div class="badge">2</div> Clean & prepare our gardenig questions and best answers</div>
# MAGIC   <div class="badge_b"><div class="badge">3</div> Use a Sentence 2 Vect model to transform our docs in a vector</div>
# MAGIC   <div class="badge_b"><div class="badge">4</div> Index the vector in our Vector database (Chroma)</div>
# MAGIC </div>
# MAGIC <br/>
# MAGIC
# MAGIC <!-- Collect usage data (view). Remove it to disable collection. View README for more details.  -->
# MAGIC <img width="1px" src="https://www.google-analytics.com/collect?v=1&gtm=GTM-NKQ8TT7&tid=UA-163989034-1&aip=1&t=event&ec=dbdemos&ea=VIEW&dp=%2F_dbdemos%2Fdata-science%2Fllm-dolly-chatbot%2F02-Data-preparation&cid=356223210893384&uid=2795718610587230">

# COMMAND ----------

# MAGIC %pip install pydantic==1.10.7

# COMMAND ----------

# DBTITLE 1,Install our vector database
# MAGIC %pip install -U chromadb==0.3.22 langchain==0.0.165  transformers==4.29.0 accelerate==0.19.0 

# COMMAND ----------

# MAGIC %run ./_resources/00-init $catalog=hive_metastore $db=dbdemos_llm

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1/ Downloading and extracting the raw dataset
# MAGIC
# MAGIC We'll focus on Gardening question, and download the gardening dataset
# MAGIC
# MAGIC - Grab the Gardening StackExchange dataset
# MAGIC - Un-7zip it (needs `7z` installed)
# MAGIC - Copy out the `Posts.xml`
# MAGIC - Parse it with `spark-xml`
# MAGIC
# MAGIC *Note that for a real-world scenario, we would be retrieving our data from external systems such as message queue (kafka), SQL database, blob storage...*

# COMMAND ----------

# DBTITLE 1,Extract the dataset using sh command
# MAGIC %sh
# MAGIC #To keep it simple, we'll download and extract the dataset using standard bash commands 
# MAGIC #Install 7zip to extract the file
# MAGIC apt-get install -y p7zip-full
# MAGIC
# MAGIC rm -rf /tmp/gardening || true
# MAGIC mkdir -p /tmp/gardening
# MAGIC cd /tmp/gardening
# MAGIC #Download & extract the gardening archive
# MAGIC curl -L https://archive.org/download/stackexchange/gardening.stackexchange.com.7z -o gardening.7z
# MAGIC 7z x gardening.7z 
# MAGIC #Move the dataset to our main bucket
# MAGIC rm -rf /dbfs/dbdemos/product/llm/gardening/raw || true
# MAGIC mkdir -p /dbfs/dbdemos/product/llm/gardening/raw
# MAGIC cp -f Posts.xml /dbfs/dbdemos/product/llm/gardening/raw

# COMMAND ----------

# DBTITLE 1,Our Q&A dataset is ready
# MAGIC %fs ls /dbdemos/product/llm/gardening/raw

# COMMAND ----------

# MAGIC %md
# MAGIC
# MAGIC ## 2/ Clean & prepare our gardenig questions and best answers 
# MAGIC
# MAGIC Let's ingest the data using [spark xml](https://github.com/databricks/spark-xml). Make sure the library is added to your cluster configuration page as a Maven library:
# MAGIC
# MAGIC Maven coordinates: `com.databricks:spark-xml_2.12:0.16.0` (we loaded it to the cluster created by dbdemos)
# MAGIC
# MAGIC We will perform some light preprocessing on the results:
# MAGIC - Keep only questions/answers with a reasonable score
# MAGIC - Parse HTML into plain text
# MAGIC - Join questions and answers to form question-answer pairs
# MAGIC
# MAGIC *Note that this pipeline is basic. For more advanced ingestion example with Databricks lakehouse, try Delta Live Table: `dbdemos.instal('dlt_loan')`*

# COMMAND ----------

# DBTITLE 1,Review our raw Q&A dataset
gardening_raw_path = demo_path+"/gardening/raw"
print(f"loading raw xml dataset under {gardening_raw_path}")
raw_gardening = spark.read.format("xml").option("rowTag", "row").load(f"{gardening_raw_path}/Posts.xml")
display(raw_gardening)

# COMMAND ----------

from pyspark.sql.functions import *

# COMMAND ----------

from bs4 import BeautifulSoup

#UDF to transform html content as text
@pandas_udf("string")
def html_to_text(html):
  return html.apply(lambda x: BeautifulSoup(x).get_text())

gardening_df =(raw_gardening
                  .filter("_Score >= 5") # keep only good answer/question
                  .filter(length("_Body") <= 1000) #remove too long questions
                  .withColumn("body", html_to_text("_Body")) #Convert html to text
                  .withColumnRenamed("_Id", "id")
                  .withColumnRenamed("_ParentId", "parent_id")
                  .select("id", "body", "parent_id"))

# Save 'raw' content for later loading of questions
gardening_df.write.mode("overwrite").saveAsTable(f"gardening_dataset")
display(spark.table("gardening_dataset"))

# COMMAND ----------

# DBTITLE 1,Assemble questions and answers
gardening_df = spark.table("gardening_dataset")

# Self-join to assemble questions and answers
qa_df = gardening_df.alias("a").filter("parent_id IS NULL") \
          .join(gardening_df.alias("b"), on=[col("a.id") == col("b.parent_id")]) \
          .select("b.id", "a.body", "b.body") \
          .toDF("answer_id", "question", "answer")
          
# Prepare the training dataset: question following with the best answers.
docs_df = qa_df.select(col("answer_id"), F.concat(col("question"), F.lit("\n\n"), col("answer"))).toDF("source", "text")
display(docs_df)

# COMMAND ----------

display(qa_df)

# COMMAND ----------

# MAGIC %md
# MAGIC
# MAGIC ### Adding a shorter version to speedup our inference 
# MAGIC
# MAGIC Our Dataset is now composed of one question followed by the best answers.
# MAGIC
# MAGIC A potential issue is that this can be a fairly long text. Using long text as context can slow down LLM inference. One option is to summarize these Q&A using a summarizer LLM and save back the result as a new field.
# MAGIC
# MAGIC This operation can take some time, this is why we'll do it once in our data preparation pipeline so that we don't have to summarize our Q&A during the inference.

# COMMAND ----------

# DBTITLE 1,Adding a summary of our data
from typing import Iterator
import pandas as pd 
from transformers import pipeline

@pandas_udf("string")
def summarize(iterator: Iterator[pd.Series]) -> Iterator[pd.Series]:
    # Load the model for summarization
    torch.cuda.empty_cache()
    summarizer = pipeline("summarization", model="sshleifer/distilbart-cnn-12-6", device_map="auto")
    def summarize_txt(text):
      if len(text) > 5000:
        return summarizer(text)[0]['summary_text']
      return text

    for serie in iterator:
        # get a summary for each row
        yield serie.apply(summarize_txt)

# We won't run it as this can take some time in the entire dataset. In this demo we set repartition to 1 as we just have 1 GPU by default.
# docs_df = docs_df.repartition(1).withColumn("text_short", summarize("text"))
docs_df.write.mode("overwrite").option("mergeSchema", "true").saveAsTable(f"gardening_training_dataset")

# COMMAND ----------

display(spark.table("gardening_training_dataset"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3/ Load our model to transform our docs to embeddings
# MAGIC
# MAGIC We will simply load a sentence to embedding model from hugging face and use it later in the chromadb client.

# COMMAND ----------

from langchain.embeddings import HuggingFaceEmbeddings

# Download model from Hugging face
hf_embed = HuggingFaceEmbeddings()

# COMMAND ----------

pip list

# COMMAND ----------

# MAGIC %md
# MAGIC
# MAGIC ## 4/ Index the documents (rows) in our vector database
# MAGIC
# MAGIC Now it's time to load the texts that have been generated, and create a searchable database of text for use in the `langchain` pipeline. <br>
# MAGIC These documents are embedded, so that later queries can be embedded too, and matched to relevant text chunks by embedding.
# MAGIC
# MAGIC - Collect the text chunks with Spark; `langchain` also supports reading chunks directly from Word docs, GDrive, PDFs, etc.
# MAGIC - Create a simple in-memory Chroma vector DB for storage
# MAGIC - Instantiate an embedding function from `sentence-transformers`
# MAGIC - Populate the database and save it

# COMMAND ----------


