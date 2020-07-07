import hail as hl
import hashlib
import logging
import os
import pandas as pd
import sys

from batch import batch_utils
from gagneurlab.gagneur_utils import GAGNEUR_BATCHES, ALL_METADATA_TSV, BAM_HEADER_PATH, GENCODE_TXDB, DOCKER_IMAGE, GCLOUD_PROJECT, GCLOUD_CREDENTIALS_LOCATION, GCLOUD_USER_ACCOUNT

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def get_sample_set_label(sample_ids):
    byte_string = ", ".join(sorted(sample_ids)).encode()
    h = hashlib.md5(byte_string).hexdigest().upper()
    return f"{len(sample_ids)}_samples_{h[:10]}"


def main():
    p = batch_utils.init_arg_parser(default_cpu=4, gsa_key_file=os.path.expanduser("~/.config/gcloud/misc-270914-cb9992ec9b25.json"))
    p.add_argument("--skip-step1", action="store_true", help="Skip count-split-reads step")
    p.add_argument("-m1", "--memory-step1", type=float, help="Batch: (optional) memory in gigabytes (eg. 3.75)", default=3.75)
    p.add_argument("-m2", "--memory-step2", type=float, help="Batch: (optional) memory in gigabytes (eg. 3.75)", default=3.75)
    p.add_argument("-t1", "--num-threads-step1", type=int, help="Num threads to use in the 'gather' job of step 1", default=4)
    p.add_argument("-t2", "--num-threads-step2", type=int, help="Num threads to use in the 'gather' job of step 2", default=4)
    p.add_argument("--metadata-tsv-path", default=ALL_METADATA_TSV, help="Table with columns: sample_id, bam_path, bai_path, batch")
    p.add_argument("batch_name", nargs="+", choices=GAGNEUR_BATCHES.keys(), help="Name of RNA-seq batch to process")
    args = p.parse_args()

    hl.init(log="/dev/null", quiet=True)

    with hl.hadoop_open(args.metadata_tsv_path) as f:
        original_samples_df = pd.read_table(f).set_index("sample_id", drop=False)

    with batch_utils.run_batch(args) as batch:

        for batch_name in args.batch_name:
            samples_df = original_samples_df
            batch_dict = GAGNEUR_BATCHES[batch_name]
            samples_df = samples_df.loc[batch_dict['samples']]
            sample_set_label = get_sample_set_label(samples_df.sample_id)

            logger.info(f"Processing {len(samples_df)} sample ids: {', '.join(samples_df.sample_id[:20])}")

            split_reads_samples = []

            split_reads_output_files = set()
            split_reads_jobs = {}

            non_split_reads_output_files = set()
            non_split_reads_jobs = {}

            j_extract_splice_junctions = None
            #j_calculate_psi_values = None

            # based on docs @ https://bioconductor.org/packages/devel/bioc/vignettes/FRASER/inst/doc/FRASER.pdf
            # step 1: count spliced reads
            # step 2: count non-spliced reads at acceptors & donors of splice junctions detected in step 1
            for step in 1, 2:
                for sample_id in samples_df.sample_id:
                    metadata_row = samples_df.loc[sample_id]

                    # set job inputs & outputs
                    input_bam, input_bai = metadata_row['bam_path'], metadata_row['bai_path']
                    if "GTEX" in sample_id:
                        output_dir = "gs://macarthurlab-rnaseq/gtex_v8/fraser_count_rna/"
                    else:
                        output_dir = f"gs://macarthurlab-rnaseq/{metadata_row['batch']}/fraser_count_rna/"


                    #input_bam = "gs://macarthurlab-rnaseq/temp/MUN_FAM5_SIBLINGMDC1A_01_R1.Aligned.sortedByCoord.out.subset.bam"
                    #input_bai = "gs://macarthurlab-rnaseq/temp/MUN_FAM5_SIBLINGMDC1A_01_R1.Aligned.sortedByCoord.out.subset.bam.bai"

                    print("Input bam: ", input_bam)
                    if step == 1:
                        output_file_path = os.path.join(output_dir, f"fraser_count_split_reads_{sample_id}.tar.gz")
                        memory = args.memory_step1
                    elif step == 2:
                        output_file_path = os.path.join(output_dir, f"fraser_count_non_split_reads_{sample_id}__{sample_set_label}.tar.gz")
                        memory = args.memory_step2

                    if step == 1:
                        split_reads_samples.append(sample_id)
                        split_reads_output_files.add(output_file_path.replace(sample_id, "*"))
                    elif step == 2:
                        non_split_reads_output_files.add(output_file_path.replace(sample_id, "*"))

                    if step == 1 and args.skip_step1:
                        continue

                    # check if output file already exists
                    if not args.force and hl.hadoop_is_file(output_file_path):
                        logger.info(f"{sample_id} output file already exists: {output_file_path}. Skipping...")
                        continue

                    if not args.local:
                        file_stats = hl.hadoop_stat(metadata_row['bam_path'])
                        bam_size = int(round(file_stats['size_bytes']/10.**9))
                        disk_size = bam_size * 2
                    else:
                        disk_size = None

                    job_label = f"Count {'split' if step == 1 else 'non-split'} reads"
                    j = batch_utils.init_job(batch, f"{job_label}: {sample_id}", cpu=args.cpu, memory=memory, disk_size=disk_size, image=DOCKER_IMAGE)
                    batch_utils.switch_gcloud_auth_to_user_account(j, GCLOUD_CREDENTIALS_LOCATION, GCLOUD_USER_ACCOUNT, GCLOUD_PROJECT)

                    j.command(f"gsutil -u {GCLOUD_PROJECT} -m cp {input_bam} {sample_id}.bam")
                    j.command(f"gsutil -u {GCLOUD_PROJECT} -m cp {input_bai} {sample_id}.bam.bai")
                    j.command(f"touch {sample_id}.bam.bai")
                    bam_path = f"{sample_id}.bam"

                    j.command(f"pwd && ls -lh && date")

                    if step == 1:
                        # count split reads
                        j.command(f"""time xvfb-run Rscript -e '
library(FRASER)
library(data.table)

sampleTable = data.table(sampleID=c("{sample_id}"), bamFile=c("{bam_path}"))
print(sampleTable)
fds = FraserDataSet(colData=sampleTable, workingDir=".", bamParam=ScanBamParam(mapqFilter=0), strandSpecific=0L)

getSplitReadCountsForAllSamples(fds)  # saves results to cache/
'""")
                    elif step == 2:
                        if sample_id in split_reads_jobs:
                            j.depends_on(split_reads_jobs[sample_id])
                        if j_extract_splice_junctions:
                            j.depends_on(j_extract_splice_junctions)

                        j.command(f"gsutil -m cp {output_file_path_splice_junctions_RDS} .")

                        # count non-split reads
                        j.command(f"""time xvfb-run Rscript -e '
library(FRASER)
library(data.table)

spliceJunctions = readRDS("{os.path.basename(output_file_path_splice_junctions_RDS)}")

sampleTable = data.table(sampleID=c("{sample_id}"), bamFile=c("{bam_path}"))
print(sampleTable)

fds = FraserDataSet(colData=sampleTable, workingDir=".", bamParam=ScanBamParam(mapqFilter=0), strandSpecific=0L)

getNonSplitReadCountsForAllSamples(fds, spliceJunctions)  # saves results to cache/
'""")
                    j.command(f"ls -lh .")
                    j.command(f"tar czf {os.path.basename(output_file_path)} cache")
                    j.command(f"gsutil -m cp {os.path.basename(output_file_path)} {output_file_path}")

                    j.command(f"echo Done: {output_file_path}")
                    j.command(f"date")

                    print("Output file path: ", output_file_path)

                    if step == 1:
                        split_reads_jobs[sample_id] = j
                    elif step == 2:
                        non_split_reads_jobs[sample_id] = j

                if len(split_reads_output_files) == 0:
                    break

                if step == 1:
                    output_file_path_splice_junctions_RDS = os.path.join(output_dir, f"spliceJunctions_{sample_set_label}.RDS")
                    if hl.hadoop_is_file(output_file_path_splice_junctions_RDS) and not args.force:
                        logger.info(f"{output_file_path_splice_junctions_RDS} file already exists. Skipping extractSpliceJunctions step...")
                        continue

                    j_extract_splice_junctions = batch_utils.init_job(batch, f"Extract splice-junctions", disk_size=30, memory=60, image=DOCKER_IMAGE)
                    batch_utils.switch_gcloud_auth_to_user_account(j_extract_splice_junctions, GCLOUD_CREDENTIALS_LOCATION, GCLOUD_USER_ACCOUNT, GCLOUD_PROJECT)

                    for j in split_reads_jobs.values():
                        j_extract_splice_junctions.depends_on(j)

                    j_extract_splice_junctions.command(f"gsutil -m cp {' '.join(split_reads_output_files)} .")
                    j_extract_splice_junctions.command(f"gsutil -m cp {BAM_HEADER_PATH} .")
                    j_extract_splice_junctions.command(f"for i in fraser_count_split_reads*.tar.gz; do tar xzf $i; done")
                    j_extract_splice_junctions.command(f"pwd && ls -lh && date && echo ------- && find cache -name '*.*'")
                    j_extract_splice_junctions.command(f"""time xvfb-run Rscript -e '
library(FRASER)
library(data.table)
library(stringr)
library(purrr)
library(BiocParallel)

file_paths = list.files(".", pattern = "fraser_count_split_reads_.*.tar.gz$")
print(file_paths)
parse_sample_id = function(x) {{ return( str_replace(x[[1]], "fraser_count_split_reads_", "")) }}
sample_ids = unlist(map(strsplit(file_paths, "[.]"), parse_sample_id))

sampleTable = data.table(sampleID=sample_ids, bamFile="{os.path.basename(BAM_HEADER_PATH)}")
print(sampleTable)

if({args.num_threads_step1} == 1) {{
    bpparam = SerialParam(log=TRUE, progressbar=FALSE)
}} else {{
    bpparam = MulticoreParam({args.num_threads_step1}, log=FALSE, progressbar=FALSE)
}}

fds = FraserDataSet(colData=sampleTable, workingDir=".", bamParam=ScanBamParam(mapqFilter=0), strandSpecific=0L)
splitCountsForAllSamples = getSplitReadCountsForAllSamples(fds, BPPARAM=bpparam)
splitCountRanges = rowRanges(splitCountsForAllSamples)
print(splitCountRanges)

saveRDS(splitCountRanges, "spliceJunctions.RDS")
'""")
                    j_extract_splice_junctions.command(f"ls -lh .")
                    j_extract_splice_junctions.command(f"gsutil -m cp spliceJunctions.RDS {output_file_path_splice_junctions_RDS}")
                    print("Output file path: ", output_file_path)

                    print("Output file path: ", output_file_path_splice_junctions_RDS)
                elif step == 2:
                    output_file_path = os.path.join(output_dir, f"calculatedPSIValues_{sample_set_label}.tar.gz")
                    if hl.hadoop_is_file(output_file_path) and not args.force:
                        logger.info(f"{output_file_path} file already exists. Skipping calculatePSIValues step...")
                        continue

                    j_calculate_psi_values = batch_utils.init_job(batch, f"Calculate PSI values", disk_size=50, cpu=(4 if args.local else 16), memory=60, image=DOCKER_IMAGE)
                    batch_utils.switch_gcloud_auth_to_user_account(j_calculate_psi_values, GCLOUD_CREDENTIALS_LOCATION, GCLOUD_USER_ACCOUNT, GCLOUD_PROJECT)

                    if j_extract_splice_junctions:
                        j_calculate_psi_values.depends_on(j_extract_splice_junctions)
                    for j in non_split_reads_jobs.values():
                        j_calculate_psi_values.depends_on(j)

                    j_calculate_psi_values.command(f"gsutil -m cp {' '.join(split_reads_output_files)} .")
                    j_calculate_psi_values.command(f"gsutil -m cp {' '.join(non_split_reads_output_files)} .")
                    j_calculate_psi_values.command(f"gsutil -m cp {output_file_path_splice_junctions_RDS} .")
                    j_calculate_psi_values.command(f"gsutil -m cp {BAM_HEADER_PATH} .")

                    j_calculate_psi_values.command(f"for i in fraser_count_split_reads*.tar.gz; do tar xzf $i; done")
                    j_calculate_psi_values.command(f"for i in fraser_count_non_split_reads*.tar.gz; do tar xzf $i; done")
                    j_calculate_psi_values.command(f"rm cache/nonSplicedCounts/Data_Analysis/spliceSiteCoordinates.RDS")
                    j_calculate_psi_values.command(f"pwd && ls -lh && date && echo ------- && find cache -name '*.*'")
                    j_calculate_psi_values.command(f"""time xvfb-run Rscript -e '
library(FRASER)
library(data.table)
library(stringr)
library(purrr)
library(BiocParallel)

splitCountRanges = readRDS("{os.path.basename(output_file_path_splice_junctions_RDS)}")
print(splitCountRanges)


file_paths = list.files(".", pattern = "fraser_count_split_reads_.*.tar.gz$")
print(file_paths)
parse_sample_id = function(x) {{ return( str_replace(x[[1]], "fraser_count_split_reads_", "")) }}
sample_ids = unlist(map(strsplit(file_paths, "[.]"), parse_sample_id))

sampleTable = data.table(sampleID=sample_ids, bamFile="{os.path.basename(BAM_HEADER_PATH)}")
print(sampleTable)

fds = FraserDataSet(colData=sampleTable, workingDir=".", bamParam=ScanBamParam(mapqFilter=0), strandSpecific=0L)
if({args.num_threads_step2}L == 1L) {{
    bpparam = SerialParam(log=TRUE, progressbar=FALSE)
}} else {{
    bpparam = MulticoreParam({args.num_threads_step2}, log=FALSE, threshold = "DEBUG", progressbar=FALSE)
}}

splitCountsForAllSamples = getSplitReadCountsForAllSamples(fds, BPPARAM=bpparam)
nonSplitCountsForAllSamples = getNonSplitReadCountsForAllSamples(fds, splitCountRanges, BPPARAM=bpparam)
fds = addCountsToFraserDataSet(fds, splitCountsForAllSamples, nonSplitCountsForAllSamples)
fds = calculatePSIValues(fds, BPPARAM=bpparam)
fds = annotateRanges(fds, GRCh=38)
saveRDS(fds, "fdsWithPSIValues.RDS")
'""")
                    j_calculate_psi_values.command(f"ls -lh .")
                    j_calculate_psi_values.command(f"tar czf {os.path.basename(output_file_path)} cache savedObjects fdsWithPSIValues.RDS")
                    j_calculate_psi_values.command(f"gsutil -m cp {os.path.basename(output_file_path)} {output_file_path}")
                    print("Output file path: ", output_file_path)


if __name__ == "__main__":
    main()
