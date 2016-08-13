#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Helper functions for Copy Number Variations (CNV).
"""

import sys
import logging
import os.path as op

import numpy as np
import numpy.ma as ma
import pandas as pd

from collections import defaultdict
from random import choice

from jcvi.utils.aws import sync_from_s3
from jcvi.algorithms.formula import get_kmeans
from jcvi.apps.base import OptionParser, ActionDispatcher, getfilesize, mkdir, sh


autosomes = ["chr{}".format(x) for x in range(1, 23)]
sexsomes = ["chrX", "chrY"]
allsomes = autosomes + sexsomes


class CopyNumberHMM(object):

    def __init__(self, workdir, betadir="beta",
                 mu=.003, sigma=10, step=.1, threshold=.2):
        self.model = self.initialize(mu=mu, sigma=sigma, step=step)
        self.workdir = workdir
        self.betadir = betadir
        if not op.exists(betadir):
            sync_from_s3("s3://hli-mv-data-science/htang/ccn/beta",
                         target_dir=betadir)
        self.mu = mu
        self.sigma = sigma
        self.step = step
        self.threshold = threshold

    def run(self, samplekey, chrs=allsomes):
        if isinstance(chrs, str):
            chrs = [chrs]
        allevents = []
        for chr in chrs:
            X, Z, clen, events = self.run_one(samplekey, chr)
            allevents.extend(events)
        return allevents

    def run_one(self, samplekey, chr):
        cov = np.fromfile("{}/{}-cn/{}.{}.cn".format(self.workdir, samplekey, samplekey, chr))
        beta = np.fromfile("beta/{}.beta".format(chr))
        std = np.fromfile("beta/{}.std".format(chr))
        # Check if the two arrays have different dimensions
        clen, blen = cov.shape[0], beta.shape[0]
        if clen < blen:
            cov = np.array(list(cov) + [np.nan] * (blen - clen))
            clen = cov.shape[0]
        assert clen == blen, "coverage and correction array not same dimension"
        normalized = cov / beta
        fixed = normalized.copy()
        fixed[np.where(std > self.threshold)] = np.nan
        X = fixed
        Z = self.predict(X)

        med_cn = np.median(fixed[np.isfinite(fixed)])
        base = med_cn if chr == "chrX" else 2
        base = base if chr != "chrY" else 1
        print chr, med_cn

        # Annotate segments
        segments = self.annotate_segments(Z)
        events = []
        for mean_cn, rr in segments:
            ss = fixed[rr[0]: rr[1]]
            realbins = np.sum(np.isfinite(ss))
            # Determine whether this is an outlier
            tag = self.tag(chr, mean_cn, rr, med_cn, base, realbins)
            if tag:
                print tag
            events.append((mean_cn, rr, tag))

        return X, Z, clen, events

    def tag(self, chr, mean_cn, rr, med_cn, base, realbins):
        around_1 = around_value(mean_cn, 1)
        around_2 = around_value(mean_cn, 2)
        if chr == "chrX":
            start, end = rr
            if med_cn < 1.5:  # Male
                # PAR ~ 2, rest ~ 1
                if end < 5000 or start > 155000:
                    if around_2:
                        return
                else:
                    if around_1:
                        return
            else:
                # All ~ 2
                if around_2:
                    return
        elif chr == "chrY":
            if med_cn < .5: # Female
                if mean_cn < .5:
                    return
            else:
                if around_1:
                    return
        else:
            if around_2:
                return
        tag = "GAIN" if mean_cn > base else "LOSS"
        mb = rr / 1000.
        msg = "[{}] {}:{}-{}Mb CN={} bins={}".format(tag, chr,
                                format_float(mb[0]), format_float(mb[1]),
                                mean_cn, realbins)
        if realbins >= 10000:
            msg += "*"
        return msg

    def initialize(self, mu, sigma, step):
        from hmmlearn import hmm

        # Initial population probability
        n = int(10 / step)
        startprob = 1. / n * np.ones(n)
        transmat = mu * np.ones((n, n))
        np.fill_diagonal(transmat, 1 - (n - 1) * mu)

        # The means of each component
        means = np.arange(0, step * n, step)
        means.resize((n, 1, 1))
        # The covariance of each component
        covars = sigma * np.ones((n, 1, 1))

        # Build an HMM instance and set parameters
        model = hmm.GaussianHMM(n_components=n, covariance_type="full")

        # Instead of fitting it from the data, we directly set the estimated
        # parameters, the means and covariance of the components
        model.startprob_ = startprob
        model.transmat_ = transmat
        model.means_ = means
        model.covars_ = covars
        return model

    def predict(self, X):
        # Handle missing values
        X = ma.masked_invalid(X)
        mask = X.mask
        dX = ma.compressed(X).reshape(-1, 1)
        dZ = self.model.predict(dX)
        Z = np.array([np.nan for i in xrange(X.shape[0])])
        Z[~mask] = dZ
        Z = ma.masked_invalid(Z)

        return Z * self.step

    def annotate_segments(self, Z):
        """ Report the copy number and start-end segment
        """
        # We need a way to go from compressed idices to original indices
        P = Z.copy()
        P[~np.isfinite(P)] = -1
        _, mapping = np.unique(np.cumsum(P >= 0), return_index=True)

        dZ = Z.compressed()
        uniq, idx = np.unique(dZ, return_inverse=True)
        segments = []
        for i, mean_cn in enumerate(uniq):
            if not np.isfinite(mean_cn):
                continue
            for rr in contiguous_regions(idx == i):
                segments.append((mean_cn, mapping[rr]))

        return segments

    def plot(self, samplekey, chrs=allsomes, color=None, dx=None):
        import matplotlib.pyplot as plt
        from jcvi.utils.brewer2mpl import get_map

        props = dict(boxstyle='round', facecolor='wheat', alpha=0.2)

        if isinstance(chrs, str):
            chrs = [chrs]
        f, axs = plt.subplots(1, len(chrs), sharey=True)
        if not isinstance(axs, np.ndarray):
            axs = np.array([axs])
        plt.tight_layout()
        if color is None:
            color = choice(get_map('Set2', 'qualitative', 8).mpl_colors)

        for chr, ax in zip(chrs, axs):
            X, Z, clen, events = self.run_one(samplekey, chr)
            ax.plot(X, ".", label="observations", ms=2, mfc=color, alpha=0.7)
            ax.plot(Z, "k.", label="hidden", ms=6)
            ax.set_xlim(0, clen)
            ax.set_ylim(0, 6)
            ax.set_xlabel("1Kb bins")
            title = "{} {}".format(samplekey.split("_")[1], chr)
            if dx:
                title += " ({})".format(dx)
            ax.set_title(title)

            # The final calls
            yy = .9
            abnormal = [x for x in events if x[-1]]
            if len(abnormal) > 5:
                yinterval = .02
                size = 10
            else:
                yinterval = .05
                size = 12
            for mean_cn, rr, event in events:
                if mean_cn > 6:
                    continue
                ax.text(np.mean(rr), mean_cn + .2, mean_cn, ha="center", bbox=props)
                if event is None:
                    continue
                ax.text(.5, yy, event.rsplit(" ", 1)[0], color='r', ha="center",
                        transform=ax.transAxes, size=size)
                yy -= yinterval

        axs[0].set_ylabel("Copy number")


def contiguous_regions(condition):
    """Finds contiguous True regions of the boolean array "condition". Returns
    a 2D array where the first column is the start index of the region and the
    second column is the end index."""

    # Find the indicies of changes in "condition"
    d = np.diff(condition)
    idx, = d.nonzero()

    # We need to start things after the change in "condition". Therefore,
    # we'll shift the index by 1 to the right.
    idx += 1

    if condition[0]:
        # If the start of condition is True prepend a 0
        idx = np.r_[0, idx]

    if condition[-1]:
        # If the end of condition is True, append the length of the array
        idx = np.r_[idx, condition.size] # Edit

    # Reshape the result into two columns
    idx.shape = (-1,2)
    return idx


def format_float(f):
    s = "{:.2f}".format(f)
    return s.rstrip('0').rstrip('.')


def around_value(s, mu, max_dev=.25):
    return mu - max_dev < s < mu + max_dev


def main():

    actions = (
        ('gcshift', 'correct cib according to GC content'),
        ('mergecn', 'compile matrix of GC-corrected copy numbers'),
        ('hmm', 'run cnv segmentation'),
        # Interact with CCN script
        ('batchccn', 'run CCN script in batch'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def hmm(args):
    """
    %prog hmm workdir sample_key

    Run CNV segmentation caller. The workdir must contain a subfolder called
    `sample_key-cn` that contains CN for each chromosome. A `beta` directory
    that contains scaler for each bin must also be present in the current
    directory.
    """
    p = OptionParser(hmm.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    workdir, sample_key = args
    model = CopyNumberHMM(workdir=workdir)
    events = model.run(sample_key)
    hmmfile = op.join(workdir, sample_key + ".seg")
    fw = open(hmmfile, "w")
    nevents = 0
    for mean_cn, rr, event in events:
        if event is None:
            continue
        print >> fw, " ".join((sample_key, event))
        nevents += 1
    fw.close()
    logging.debug("A total of {} aberrant events written to `{}`"\
                    .format(nevents, hmmfile))


def batchccn(args):
    """
    %prog batchccn test.csv

    Run CCN script in batch. Write makefile.
    """
    from jcvi.apps.grid import MakeManager

    p = OptionParser(batchccn.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    csvfile, = args
    mm = MakeManager()
    pf = op.basename(csvfile).split(".")[0]
    mkdir(pf)

    header = open(csvfile).next()
    header = None if header.strip().endswith(".bam") else "infer"
    logging.debug("Header={}".format(header))
    df = pd.read_csv(csvfile, header=header)
    cmd = "perl /mnt/software/ccn_gcn_hg38_script/ccn_gcn_hg38.pl"
    cmd += " -n {} -b {}"
    cmd += " -o {} -r hg38".format(pf)
    for i, (sample_key, bam) in df.iterrows():
        cmdi = cmd.format(sample_key, bam)
        outfile = "{}/{}/{}.ccn".format(pf, sample_key, sample_key)
        mm.add(csvfile, outfile, cmdi)
    mm.write()


def mergecn(args):
    """
    %prog mergecn FACE.csv

    Compile matrix of GC-corrected copy numbers. Place a bunch of folders in the
    csv file. Each folder will be scanned, one chromosomes after another.
    """
    p = OptionParser(mergecn.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    csvfile, = args
    samples = [x.replace("-cn", "").strip().strip("/") for x in open(csvfile)]
    betadir = "beta"
    mkdir(betadir)
    for seqid in allsomes:
        names = [op.join(s + "-cn", "{}.{}.cn".format(s, seqid)) \
                    for s in samples]
        arrays = [np.fromfile(name, dtype=np.float) for name in names]
        shapes = [x.shape[0] for x in arrays]
        med_shape = np.median(shapes)
        arrays = [x for x in arrays if x.shape[0] == med_shape]
        ploidy = 2 if seqid not in ("chrY", "chrM") else 1
        if seqid in sexsomes:
            chr_med = [np.median([x for x in a if x > 0]) for a in arrays]
            chr_med = np.array(chr_med)
            idx = get_kmeans(chr_med, k=2)
            zero_med = np.median(chr_med[idx == 0])
            one_med = np.median(chr_med[idx == 1])
            logging.debug("K-means with {} c0:{} c1:{}".\
                    format(seqid, zero_med, one_med))
            higher_idx = 1 if one_med > zero_med else 0
            # Use the higher mean coverage componen
            arrays = np.array(arrays)[idx == higher_idx]
        arrays = [[x] for x in arrays]
        ar = np.concatenate(arrays)
        print seqid, ar.shape
        rows, columns = ar.shape
        beta = []
        std = []
        for j in xrange(columns):
            a = ar[:, j]
            beta.append(np.median(a))
            std.append(np.std(a) / np.mean(a))
        beta = np.array(beta) / ploidy
        betafile = op.join(betadir, "{}.beta".format(seqid))
        beta.tofile(betafile)
        stdfile = op.join(betadir, "{}.std".format(seqid))
        std = np.array(std)
        std.tofile(stdfile)
        logging.debug("Written to `{}`".format(betafile))
        ar.tofile("{}.bin".format(seqid))


def load_cib(cibfile, n=1000):
    cibgzfile = cibfile + ".gz"
    if not op.exists(cibfile) or getfilesize(cibfile) < getfilesize(cibgzfile):
        cibfile = cibgzfile
    if cibfile.endswith(".gz"):
        sh("pigz -d -k -f {}".format(cibfile))
        cibfile = cibfile.replace(".gz", "")
    cib = np.fromfile(cibfile, dtype=np.int8) + 128
    rm = pd.rolling_mean(cib, n, min_periods=n / 2)
    a = rm[n - 1::n].copy()
    del cib
    del rm
    return a


def build_gc_array(fastafile="/mnt/ref/hg38.upper.fa",
                   gcdir="gc", n=1000):
    from pyfasta import Fasta
    f = Fasta(fastafile)
    mkdir(gcdir)
    for seqid in allsomes:
        c = np.array(f[seqid])
        gc = (c == 'G') | (c == 'C')  # If base is GC
        rr = ~(c == 'N')              # If base is real
        mgc = pd.rolling_sum(gc, n, min_periods=n / 2)[n - 1::n]
        mrr = pd.rolling_sum(rr, n, min_periods=n / 2)[n - 1::n]
        gc_pct = np.rint(mgc * 100 / mrr)
        gc_pct = np.asarray(gc_pct, dtype=np.uint8)
        arfile = op.join(gcdir, "{}.{}.gc".format(seqid, n))
        gc_pct.tofile(arfile)
        print >> sys.stderr, seqid, gc_pct, arfile


def gcshift(args):
    """
    %prog gcshift \
        s3://hli-bix-us-west-2/kubernetes/wf-root-test/102340_NA12878/lpierce-ccn_gcn-v2/ \
        102340_NA12878

    Download CCN output folder and convert cib to copy number per 1Kb.
    """
    p = OptionParser(gcshift.__doc__)
    p.add_option("--binsize", default=1000, type="int",
                 help="Window size along chromosome")
    p.add_option("--cleanup", default=False, action="store_true",
                 help="Clean up downloaded s3 folder")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    s3dir, sample_key = args
    n = opts.binsize
    cndir = sample_key + "-cn"
    if op.exists(cndir):
        logging.debug("Directory {} exists. Skipped.".format(cndir))
        return

    gcdir = "gc"
    if not op.exists(gcdir):
        build_gc_array(n=n)

    if s3dir.startswith("s3://"):
        sync_from_s3(s3dir, target_dir=sample_key)
    assert op.exists(sample_key), "Directory {} doesn't exist!"\
                    .format(sample_key)

    # Build GC correction table
    gc_bin = defaultdict(list)
    gc_med = {}
    coverage = []

    for seqid in allsomes:
        gcfile = op.join(gcdir, "{}.{}.gc".format(seqid, n))
        gc = np.fromfile(gcfile, dtype=np.uint8)
        cibfile = op.join(sample_key, "{}.{}.cib".format(sample_key, seqid))
        cib = load_cib(cibfile)
        print >> sys.stderr, seqid, gc.shape[0], cib.shape[0]
        if seqid in autosomes:
            for gci, k in zip(gc, cib):
                gc_bin[gci].append(k)
        coverage.append((seqid, gc, cib))

    for gci, k in gc_bin.items():
        nonzero_k = [x for x in k if x]
        gc_med[gci] = med = np.median(nonzero_k) / 2
        print >> sys.stderr, gci, len(nonzero_k), med

    mkdir(cndir)
    apply_fun = np.vectorize(gc_med.get)
    # Apply the GC correction over coverage
    for seqid, gc, cib in coverage:
        nitems = cib.shape[0]
        beta = apply_fun(gc[:nitems])
        cn = cib / beta
        cnfile = op.join(cndir, "{}.{}.cn".format(sample_key, seqid))
        cn.tofile(cnfile)

    if opts.cleanup:
        import shutil
        shutil.rmtree(sample_key)


if __name__ == '__main__':
    main()