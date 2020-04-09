# Transfer files from a Terra bucket to the macarthurlab-rnaseq bucket

import argparse
import logging
import os
import pprint

from firecloud import api

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s')
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--source-namespace", default="macarthurlab-rnaseq-terra")
    p.add_argument("--dest-bucket", default="macarthurlab-rnaseq")
    p.add_argument("source_workspace")
    p.add_argument("batch_name")
    args = p.parse_args()

    return args


def run(cmd):
    logger.info(cmd)
    os.system(cmd)


def gsutil_cp(source, dest):
    run("gsutil -m cp -n %s  %s" % (source, dest))


def copy_hg19_bams(args):
    # returns a list of dictionaries. Each dictionary is a row in one of the metadata tables.
    entities = api.get_entities_with_type(args.source_namespace, args.source_workspace).json()

    # if the workspace has both sample, sample_set, and participant tables, the list includes rows from all of them, so filter to just the sample records
    sample_entities = [e for e in entities if e['entityType'] == "sample"]
    # for each sample, get the relevant columns including cram path, and add them to a tsv file string for upload
    ws_total_bams = ws_total_bais = 0
    for e in sample_entities:
        sample_name = e['name']
        attr = e['attributes']
        # find which column in this workspace has the cram/bam path. Then copy it to the 'cram_path'/'crai_path' columns. I previously got this list of column names by retrieving all tables from all workspaces and looking for any column name with "cram" or "bam"
        found_bam_path = False
        for bam_key, bai_key in [
            ('bam_file', 'bai_file'),
            ('cram_or_bam_path', 'crai_or_bai_path'),
        ]:
            if attr.get(bam_key): #  and os.system("gsutil -u seqr-project ls " + attr[bam_key]) == 0:
                found_bam_path = True
                attr['hg19_bam_path'] = attr.get(bam_key)
                attr['hg19_bai_path'] = attr.get(bai_key)
                if attr.get('hg19_bam_path'):
                    break

        else:
            print("Missing bam path for: "  + sample_name)
            continue

        ws_total_bams += 1
        ws_total_bais += 1

        dest = "gs://%s/%s/hg19_bams/" % (args.dest_bucket, args.batch_name)
        gsutil_cp(attr['hg19_bam_path'], dest)
        gsutil_cp(attr['hg19_bam_path'].replace(".bam", "*bai"), dest)

def main():
    args = parse_args()
    logger.info("Args:\n" + pprint.pformat(args.__dict__))

    ws = api.get_workspace(args.source_namespace, args.source_workspace).json()
    logger.info("Workspace:\n" + pprint.pformat(ws))

    source_bucket = ws['workspace']['bucketName']
    bucket_and_batch_name = (args.dest_bucket, args.batch_name)

    # hg19 bams
    copy_hg19_bams(args)

    # star
    dest = "gs://%s/%s/star/" % bucket_and_batch_name
    gsutil_cp("gs://%s/**star_out/*.Aligned.sortedByCoord.out.bam" % source_bucket, dest)
    gsutil_cp("gs://%s/**star_out/*.Aligned.sortedByCoord.out.bam.bai" % source_bucket,  dest)
    gsutil_cp("gs://%s/**star_out/*.Chimeric.out.junction.gz" % source_bucket,  dest)
    gsutil_cp("gs://%s/**star_out/*.Log.final.out" % source_bucket,  dest)
    gsutil_cp("gs://%s/**star_out/*.Log.out" % source_bucket,  dest)
    gsutil_cp("gs://%s/**star_out/*.Log.progress.out" % source_bucket,  dest)
    gsutil_cp("gs://%s/**star_out/*.ReadsPerGene.out.tab.gz" % source_bucket,  dest)
    gsutil_cp("gs://%s/**star_out/*.SJ.out.tab.gz" % source_bucket,  dest)

    # rnaseqc
    dest = "gs://%s/%s/rnaseqc/" % bucket_and_batch_name
    gsutil_cp("gs://%s/**call-rnaseqc2/*.metrics.tsv" % source_bucket, dest)
    gsutil_cp("gs://%s/**call-rnaseqc2/*.fragmentSizes.txt" % source_bucket, dest)
    gsutil_cp("gs://%s/**call-rnaseqc2/*.exon_reads.gct.gz" % source_bucket, dest)
    gsutil_cp("gs://%s/**call-rnaseqc2/*.gene_reads.gct.gz" % source_bucket, dest)
    gsutil_cp("gs://%s/**call-rnaseqc2/*.gene_tpm.gct.gz" % source_bucket, dest)

    # fastqc
    dest = "gs://%s/%s/fastqc/zip/" % bucket_and_batch_name
    gsutil_cp("gs://%s/**_fastqc.zip" % source_bucket, dest)

    logger.info("Done")


if __name__ == "__main__":
    main()