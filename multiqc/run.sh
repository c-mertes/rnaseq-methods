multiqc -f -m star -m fastqc -m rna_seqc --filename all.html . 
#multiqc -f -m rna_seqc --filename all.html . 
#for sample_list in *_list.txt; do 
#    multiqc -f -m star -m fastqc --filename $(echo $sample_list | sed 's/_list.txt//').html --file-list $sample_list . 
#done