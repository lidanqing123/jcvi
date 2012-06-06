#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Patch the sequences of one assembly using sequences from another assembly. This
is tested on merging the medicago WGS assembly with the clone-by-clone assembly.
"""

import os.path as op
import sys
import logging

from itertools import groupby
from optparse import OptionParser

from jcvi.formats.bed import Bed, BedLine, complementBed, mergeBed, fastaFromBed
from jcvi.formats.fasta import Fasta
from jcvi.formats.sizes import Sizes
from jcvi.utils.range import range_parse
from jcvi.formats.base import must_open, FileMerger
from jcvi.apps.base import ActionDispatcher, debug, sh, mkdir
debug()


def main():

    actions = (
        ('refine', 'find gaps within or near breakpoint regions'),
        ('patcher', 'given om alignment, prepare the patchers'),
        ('fill', 'perform gap filling using one assembly vs the other'),
        ('install', 'install patches into backbone'),
        ('tips', 'append telomeric sequences based on patchers and complements'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def tips(args):
    """
    %prog tips patchers.bed complements.bed original.fasta backbone.fasta

    Append telomeric sequences based on patchers and complements.
    """
    p = OptionParser(tips.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 4:
        sys.exit(not p.print_help())

    pbedfile, cbedfile, sizesfile, bbfasta = args

    pbed = Bed(pbedfile, sorted=False)
    cbed = Bed(cbedfile, sorted=False)

    complements = dict()
    for object, beds in groupby(cbed, key=lambda x: x.seqid):
        beds = list(beds)
        complements[object] = beds

    sizes = Sizes(sizesfile).mapping
    bbsizes = Sizes(bbfasta).mapping
    tbeds = []

    for object, beds in groupby(pbed, key=lambda x: x.accn):
        beds = list(beds)
        startbed, endbed = beds[0], beds[-1]
        start_id, end_id = startbed.seqid, endbed.seqid
        if startbed.start == 1:
            start_id = None
        if endbed.end == sizes[end_id]:
            end_id = None
        print >> sys.stderr, object, start_id, end_id
        if start_id:
            b = complements[start_id][0]
            b.accn = object
            tbeds.append(b)
        tbeds.append(BedLine("\t".join(str(x) for x in \
                        (object, 0, bbsizes[object], object, 1000, "+"))))
        if end_id:
            b = complements[end_id][-1]
            b.accn = object
            tbeds.append(b)

    tbed = Bed()
    tbed.extend(tbeds)

    tbedfile = "tips.bed"
    tbed.print_to_file(tbedfile)


def fill(args):
    """
    %prog fill gaps.bed bad.fasta

    Perform gap filling of one assembly (bad) using sequences from another.
    """
    p = OptionParser(fill.__doc__)
    p.add_option("--extend", default=2000, type="int",
                 help="Extend seq flanking the gaps [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    gapsbed, badfasta = args
    Ext = opts.extend

    gapsbed = mergeBed(gapsbed, d=2 * Ext, nms=True)

    bed = Bed(gapsbed)
    sizes = Sizes(badfasta).mapping
    pf = gapsbed.rsplit(".", 1)[0]
    extbed = pf + ".ext.bed"
    fw = open(extbed, "w")
    for b in bed:
        gapname = b.accn
        start, end = max(0, b.start - Ext - 1), b.start - 1
        print >> fw, "\t".join(str(x) for x in \
                             (b.seqid, start, end, gapname + "L"))
        start, end = b.end, min(sizes[b.seqid], b.end + Ext)
        print >> fw, "\t".join(str(x) for x in \
                             (b.seqid, start, end, gapname + "R"))
    fw.close()

    fastaFromBed(extbed, badfasta, name=True)


def install(args):
    """
    %prog install patchers.bed patchers.fasta backbone.fasta alt.fasta

    Install patches into backbone, using sequences from alternative assembly.
    The patches sequences are generated via jcvi.assembly.patch.fill().

    The output is a bedfile that can be converted to AGP using
    jcvi.formats.agp.frombed().
    """
    from jcvi.apps.base import blast
    from jcvi.formats.blast import BlastSlow
    from jcvi.formats.fasta import SeqIO

    p = OptionParser(install.__doc__)
    p.add_option("--rclip", default=1, type="int",
            help="Pair ID is derived from rstrip N chars [default: %default]")
    p.add_option("--maxsize", default=1000000, type="int",
            help="Maximum size of patchers to be replaced [default: %default]")
    p.add_option("--prefix", help="Prefix of the new object [default: %default]")
    p.add_option("--strict", default=False, action="store_true",
            help="Only update if replacement has no gaps [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 4:
        sys.exit(not p.print_help())

    pbed, pfasta, bbfasta, altfasta = args
    Max = opts.maxsize  # Max DNA size to replace gap
    rclip = opts.rclip
    prefix = opts.prefix

    blastfile = blast([altfasta, pfasta,"--wordsize=100", "--pctid=99"])
    order = Bed(pbed).order

    beforebed, afterbed = "before.bed", "after.bed"
    fwa = open(beforebed, "w")
    fwb = open(afterbed, "w")

    key1 = lambda x: x.query
    key2 = lambda x: x.query[:-rclip] if rclip else key1
    data = BlastSlow(blastfile)

    for pe, lines in groupby(data, key=key2):
        lines = list(lines)
        if len(lines) != 2:
            continue

        a, b = lines

        aquery, bquery = a.query, b.query
        asubject, bsubject = a.subject, b.subject
        if asubject != bsubject:
            continue

        astrand, bstrand = a.orientation, b.orientation
        assert aquery[-1] == 'L' and bquery[-1] == 'R', str((aquery, bquery))

        ai, ax = order[aquery]
        bi, bx = order[bquery]
        qstart, qstop = ax.start + a.qstart - 1, bx.start + b.qstop - 1

        if astrand == '+' and bstrand == '+':
            sstart, sstop = a.sstart, b.sstop

        elif astrand == '-' and bstrand == '-':
            sstart, sstop = b.sstart, a.sstop

        else:
            continue

        if sstart > sstop:
            continue

        if sstop > sstart + Max:
            continue

        name = aquery[:-1] + "LR"
        print >> fwa, "\t".join(str(x) for x in \
                    (ax.seqid, qstart - 1, qstop, name, 1000, "+"))
        print >> fwb, "\t".join(str(x) for x in \
                    (asubject, sstart - 1, sstop, name, 1000, astrand))

    fwa.close()
    fwb.close()

    beforefasta = fastaFromBed(beforebed, bbfasta, name=True, stranded=True)
    afterfasta = fastaFromBed(afterbed, altfasta, name=True, stranded=True)

    # Exclude the replacements that contain more Ns than before
    ah = SeqIO.parse(beforefasta, "fasta")
    bh = SeqIO.parse(afterfasta, "fasta")
    count_Ns = lambda x: x.seq.count('n') + x.seq.count('N')
    exclude = set()
    for arec, brec in zip(ah, bh):
        an = count_Ns(arec)
        bn = count_Ns(brec)
        if opts.strict:
            if bn == 0:
                continue

        elif bn < an:
            continue

        id = arec.id
        exclude.add(id)

    logging.debug("Ignore {0} updates because of decreasing quality."\
                    .format(len(exclude)))

    abed = Bed(beforebed, sorted=False)
    bbed = Bed(afterbed, sorted=False)
    abed = [x for x in abed if x.accn not in exclude]
    bbed = [x for x in bbed if x.accn not in exclude]

    abedfile = "before.filtered.bed"
    bbedfile = "after.filtered.bed"
    afbed = Bed()
    afbed.extend(abed)
    bfbed = Bed()
    bfbed.extend(bbed)

    afbed.print_to_file(abedfile)
    bfbed.print_to_file(bbedfile)

    # Shuffle the two bedfiles together
    sizesfile = Sizes(bbfasta).filename
    shuffled = "shuffled.bed"

    abedfile = complementBed(abedfile, sizesfile)
    afbed = Bed(abedfile)
    all = []
    j = 0  # index in bfbed
    cj = 0  # index of new molecule

    for chr, beds in groupby(afbed, key=lambda x: x.seqid):
        beds = list(beds)
        cj += 1
        if prefix:
            accn = prefix + str(cj)

        b = beds[0]
        if prefix:
            b.accn = accn
        all.append(b)
        for b in beds[1:]:
            nb = bfbed[j]
            if prefix:
                b.accn = nb.accn = accn
            all.append(nb)
            j += 1
            all.append(b)

    shuffledbed = Bed()
    shuffledbed.extend(all)
    shuffledbed.print_to_file(shuffled)


def refine(args):
    """
    %prog refine breakpoints.bed gaps.bed

    Find gaps within or near breakpoint region.
    """
    p = OptionParser(refine.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    breakpointsbed, gapsbed = args
    cmd = "intersectBed -wao -a {0} -b {1}".format(breakpointsbed, gapsbed)

    pf = "{0}.{1}".format(breakpointsbed.split(".")[0], gapsbed.split(".")[0])
    ingapsbed = pf + ".bed"
    sh(cmd, outfile=ingapsbed)

    fp = open(ingapsbed)
    data = [x.split() for x in fp]
    nogapsbed = pf + ".nogaps.bed"
    largestgapsbed = pf + ".largestgaps.bed"
    nogapsfw = open(nogapsbed, "w")
    largestgapsfw = open(largestgapsbed, "w")
    for b, gaps in groupby(data, key=lambda x: x[:3]):
        gaps = list(gaps)
        if len(gaps) == 1 and gaps[0][3] == ".":
            gap = gaps[0]
            assert gap[4] == "-1"
            print >> nogapsfw, "\t".join(b)
            continue

        gaps = [(int(x[-1]), x) for x in gaps]
        maxgap = max(gaps)[1]
        print >> largestgapsfw, "\t".join(maxgap[3:])

    nogapsfw.close()
    largestgapsfw.close()

    closestgapsbed = pf + ".closestgaps.bed"
    closestgapsfw = open(closestgapsbed, "w")
    cmd = "closestBed -a {0} -b {1}".format(nogapsbed, gapsbed)
    cmd += " | cut -f4-7"
    sh(cmd, outfile=closestgapsbed)

    refinedbed = pf + ".refined.bed"
    FileMerger([largestgapsbed, closestgapsbed], outfile=refinedbed).merge()

    # Clean-up
    cmd = "rm -f " + " ".join([nogapsbed, largestgapsbed, closestgapsbed])
    sh(cmd)


def merge_ranges(beds):

    m = [x.accn for x in beds]

    mr = [range_parse(x) for x in m]
    mc = set(x.seqid for x in mr)
    if len(mc) != 1:
        logging.error("Multiple seqid found in pocket. Aborted.")
        return

    mc = list(mc)[0]
    ms = min(x.start for x in mr)
    me = max(x.end for x in mr)

    neg_strands = sum(1 for x in beds if x.strand == '-')
    pos_strands = len(beds) - neg_strands
    strand = '-' if neg_strands > pos_strands else '+'

    return mc, ms, me, strand


def patcher(args):
    """
    %prog patcher backbone.bed other.bed

    Given optical map alignment, prepare the patchers. Use --backbone to suggest
    which assembly is the major one, and the patchers will be extracted from
    another assembly.
    """
    from jcvi.formats.bed import uniq

    p = OptionParser(patcher.__doc__)
    p.add_option("--backbone", default="OM",
                 help="Prefix of the backbone assembly [default: %default]")
    p.add_option("--object", default="object",
                 help="New object name [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    backbonebed, otherbed = args
    backbonebed = uniq([backbonebed])
    otherbed = uniq([otherbed])

    bb = opts.backbone
    pf = backbonebed.split(".")[0]
    key = lambda x: (x.seqid, x.start, x.end)
    is_bb = lambda x: x.startswith(bb)

    # Make a uniq bed keeping backbone at redundant intervals
    cmd = "intersectBed -v -wa"
    cmd += " -a {0} -b {1}".format(otherbed, backbonebed)
    outfile = otherbed.rsplit(".", 1)[0] + ".not." + backbonebed
    sh(cmd, outfile=outfile)

    uniqbed = Bed()
    uniqbedfile = pf + ".merged.bed"
    uniqbed.extend(Bed(backbonebed))
    uniqbed.extend(Bed(outfile))
    uniqbed.print_to_file(uniqbedfile, sorted=True)

    # Condense adjacent intervals, allow some chaining
    bed = uniqbed
    key = lambda x: range_parse(x.accn).seqid

    bed_fn = pf + ".patchers.bed"
    bed_fw = open(bed_fn, "w")

    for k, sb in groupby(bed, key=key):
        sb = list(sb)
        chr, start, end, strand = merge_ranges(sb)

        id = "{0}:{1}-{2}".format(chr, start, end)
        print >> bed_fw, "\t".join(str(x) for x in \
                (chr, start, end, opts.object, 1000, strand))

    bed_fw.close()


if __name__ == '__main__':
    main()
