"""GATK variant calling -- MuTect2.
"""
from distutils.version import LooseVersion
import os

from bcbio import bam, broad, utils
from bcbio.distributed.transaction import file_transaction
from bcbio.pipeline import config_utils
from bcbio.pipeline.shared import subset_variant_regions
from bcbio.pipeline import datadict as dd
from bcbio.provenance import do
from bcbio.variation import annotation, bamprep, bedutils, gatk, vcfutils, ploidy

def _add_tumor_params(paired, items, gatk_type):
    """Add tumor/normal BAM input parameters to command line.
    """
    params = []
    if not paired:
        raise ValueError("Specified MuTect2 calling but 'tumor' phenotype not present in batch\n"
                         "https://bcbio-nextgen.readthedocs.org/en/latest/contents/"
                         "pipelines.html#cancer-variant-calling\n"
                         "for samples: %s" % ", " .join([dd.get_sample_name(x) for x in items]))
    if gatk_type == "gatk4":
        params += ["-I", paired.tumor_bam]
        params += ["--tumor-sample", paired.tumor_name]
    else:
        params += ["-I:tumor", paired.tumor_bam]
    if paired.normal_bam is not None:
        if gatk_type == "gatk4":
            params += ["-I", paired.normal_bam]
            params += ["--normal-sample", paired.normal_name]
        else:
            params += ["-I:normal", paired.normal_bam]
    if paired.normal_panel is not None:
        if gatk_type == "gatk4":
            params += ["--panel-of-normals", paired.normal_panel]
        else:
            params += ["--normal_panel", paired.normal_panel]
    return params

def _add_region_params(region, out_file, items, gatk_type):
    """Add parameters for selecting by region to command line.
    """
    params = []
    variant_regions = bedutils.population_variant_regions(items)
    region = subset_variant_regions(variant_regions, region, out_file, items)
    if region:
        if gatk_type == "gatk4":
            params += ["-L", bamprep.region_to_gatk(region), "--interval-set-rule", "INTERSECTION"]
        else:
            params += ["-L", bamprep.region_to_gatk(region), "--interval_set_rule", "INTERSECTION"]
    params += gatk.standard_cl_params(items)
    return params

def _add_assoc_params(assoc_files):
    params = []
    if assoc_files.get("dbsnp"):
        params += ["--dbsnp", assoc_files["dbsnp"]]
    if assoc_files.get("cosmic"):
        params += ["--cosmic", assoc_files["cosmic"]]
    return params

def _prep_inputs(align_bams, ref_file, items):
    """Ensure inputs to calling are indexed as expected.
    """
    broad_runner = broad.runner_from_path("picard", items[0]["config"])
    broad_runner.run_fn("picard_index_ref", ref_file)
    for x in align_bams:
        bam.index(x, items[0]["config"])

def mutect2_caller(align_bams, items, ref_file, assoc_files,
                       region=None, out_file=None):
    """Call variation with GATK's MuTect2.

    This requires the full non open-source version of GATK 3.5+.
    """
    if out_file is None:
        out_file = "%s-variants.vcf.gz" % utils.splitext_plus(align_bams[0])[0]
    if not utils.file_exists(out_file):
        broad_runner = broad.runner_from_config(items[0]["config"])
        gatk_type = broad_runner.gatk_type()
        _prep_inputs(align_bams, ref_file, items)
        with file_transaction(items[0], out_file) as tx_out_file:
            params = ["-T", "Mutect2" if gatk_type == "gatk4" else "MuTect2",
                      "-R", ref_file,
                      "--annotation", "ClippingRankSumTest",
                      "--annotation", "DepthPerSampleHC"]
            for a in annotation.get_gatk_annotations(items[0]["config"], include_baseqranksum=False):
                params += ["--annotation", a]
            # Avoid issues with BAM CIGAR reads that GATK doesn't like
            if gatk_type == "gatk4":
                params += ["--read-validation-stringency", "LENIENT"]
            paired = vcfutils.get_paired_bams(align_bams, items)
            params += _add_tumor_params(paired, items, gatk_type)
            params += _add_region_params(region, out_file, items, gatk_type)
            # Avoid adding dbSNP/Cosmic so they do not get fed to variant filtering algorithm
            # Not yet clear how this helps or hurts in a general case.
            #params += _add_assoc_params(assoc_files)
            params += ["-ploidy", str(ploidy.get_ploidy(items, region))]
            resources = config_utils.get_resources("mutect2", items[0]["config"])
            if "options" in resources:
                params += [str(x) for x in resources.get("options", [])]
            assert LooseVersion(broad_runner.gatk_major_version()) >= LooseVersion("3.5"), \
                "Require full version of GATK 3.5+ for mutect2 calling"
            broad_runner.new_resources("mutect2")
            gatk_cmd = broad_runner.cl_gatk(params, os.path.dirname(tx_out_file))
            if gatk_type == "gatk4":
                tx_raw_file = "%s-raw%s" % utils.splitext_plus(tx_out_file)
                filter_cmd = _mutect2_filter(broad_runner, tx_raw_file, tx_out_file)
                cmd = "{gatk_cmd} -O {tx_raw_file} && {filter_cmd}"
            else:
                cmd = "{gatk_cmd} | bgzip -c > {tx_out_file}"
            do.run(cmd.format(**locals()), "MuTect2")

    return vcfutils.bgzip_and_index(out_file, items[0]["config"])

def _mutect2_filter(broad_runner, in_file, out_file):
    """Filter of MuTect2 calls, a separate step in GATK4.
    """
    params = ["-T", "FilterMutectCalls", "--variant", in_file, "--output", out_file]
    return broad_runner.cl_gatk(params, os.path.dirname(out_file))
