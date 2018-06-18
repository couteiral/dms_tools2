"""
===================
pacbio
===================

Tools for processing PacBio sequencing data.
"""


import os
import io
import math
import subprocess
import collections
import tempfile
import numbers

import regex
import numpy
import pandas
import pysam

# import dms_tools2.plot to set plotting contexts / themes
import dms_tools2
from dms_tools2.plot import COLOR_BLIND_PALETTE
from dms_tools2.plot import COLOR_BLIND_PALETTE_GRAY
import matplotlib.pyplot as plt
from plotnine import *


class CCS:
    """Class to handle results of ``ccs``.

    Holds results of PacBio ``ccs``.
    Has been tested on output of ``ccs`` version 3.0.0.

    This class reads all data into memory, and so you
    may need a lot of RAM if `bamfile` is large.

    Args:
        `samplename` (str)
            Sample or sequencing run
        `bamfile` (str)
            BAM file created by ``ccs``
        `reportfile` (str or `None`)
            Report file created by ``ccs``, or
            `None` if you have no reports.

    Attributes:
        `samplename` (str)
            Name set at initialization
        `bamfile` (str)
            ``ccs`` BAM file set at initialization
        `reportfile` (str or `None`)
            ``ccs`` report file set at initialization
        `zmw_report` (pandas.DataFrame or `None`):
            ZMW stats in `reportfile`, or `None` if no
            `reportfile`. Columns are *status*, *number*,
            *percent*, and *fraction*.
        `subread_report` (pandas.DataFrame or `None`)
            Like `zmw_report` but for subreads.
        `df` (pandas.DataFrame)
            The CCSs in `bamfile`. Each row is a different CCS
            On creation, there will be the following columns (you
            can modify to add more): 

              - "name": the name of the CCS
              - "samplename": the sample as set via `samplename`
              - "CCS": the circular consensus sequence
              - "CCS_qvals": the Q-values as a numpy array
              - "passes": the number of passes of the CCS
              - "CCS_accuracy": the accuracy of the CCS
              - "CCS_length": the length of the CCS

    Here is an example.

    First, define the sequences, quality scores,
    and names for 3 example sequences. The names indicate
    the barcodes, the accuracy of the barcode, and the polarity.
    Two of the sequences have the desired termini and
    a barcode. The other does not. Note that the second
    sequence has an extra nucleotide at each end, this
    will turn out to be fine with the `match_str` we write.
    The second sequence is also reverse complemented:

    >>> termini5 = 'ACG'
    >>> termini3 = 'CTT'
    >>> ccs_seqs = [
    ...         {'name':'barcoded_TTC_0.999_plus',
    ...          'seq':termini5 + 'TTC' + 'ACG' + termini3,
    ...          'qvals':'?' * 12,
    ...         },
    ...         {'name':'barcoded_AGA_0.995_minus',
    ...          'seq':dms_tools2.utils.reverseComplement(
    ...                'T' + termini5 + 'AGA' + 'GCA' + termini3 + 'A'),
    ...          'qvals':''.join(reversed('?' * 4 + '5?9' + '?' * 7)),
    ...         },
    ...         {'name':'invalid',
    ...          'seq':'GGG' + 'CAT' + 'GCA' + termini3,
    ...          'qvals':'?' * 12,
    ...         }
    ...         ]
    >>> for iccs in ccs_seqs:
    ...     iccs['accuracy'] = qvalsToAccuracy(iccs['qvals'], encoding='sanger')

    Now place these in a block of text that meets the
    `CCS SAM specification <https://github.com/PacificBiosciences/unanimity/blob/develop/doc/PBCCS.md>`_:

    >>> sam_template = '\\t'.join([
    ...        '{0[name]}',
    ...        '4', '*', '0', '255', '*', '*', '0', '0',
    ...        '{0[seq]}',
    ...        '{0[qvals]}',
    ...        'np:i:6',
    ...        'rq:f:{0[accuracy]}',
    ...        ])
    >>> samtext = '\\n'.join([sam_template.format(iccs) for
    ...                      iccs in ccs_seqs])

    Create small SAM file with these sequences, then
    convert to BAM file used to initialize a `CCS` object
    (note this requires ``samtools`` to be installed):

    >>> samfile = '_temp.sam'
    >>> bamfile = '_temp.bam'
    >>> with open(samfile, 'w') as f:
    ...     _ = f.write(samtext)
    >>> _ = subprocess.check_call(['samtools', 'view',
    ...         '-b', '-o', bamfile, samfile])
    >>> ccs = CCS('test', bamfile, None)
    >>> os.remove(samfile)
    >>> os.remove(bamfile)

    Check `ccs.df` has correct names, samplename, CCS sequences,
    and columns:

    >>> set(ccs.df.name) == {s['name'] for s in ccs_seqs}
    True
    >>> all(ccs.df.samplename == 'test')
    True
    >>> set(ccs.df.CCS) == {s['seq'] for s in ccs_seqs}
    True
    >>> set(ccs.df.columns) == {'CCS', 'CCS_qvals', 'name',
    ...         'passes', 'CCS_accuracy', 'CCS_length', 'samplename'}
    True

    Use :meth:`matchSeqs` to match sequences with expected termini
    and define barcodes and reads in these:

    >>> match_str = (termini5 + '(?P<barcode>N{3})' +
    ...         '(?P<read>N+)' + termini3)
    >>> ccs.df = matchSeqs(ccs.df, match_str, 'CCS', 'barcoded')

    This matching adds new columns to the new `ccs.df`:

    >>> set(ccs.df.columns) >= {'barcode', 'barcode_qvals',
    ...         'barcode_accuracy', 'read', 'read_qvals',
    ...         'read_accuracy', 'barcoded', 'barcoded_polarity'}
    True

    Now make sure `df` indicates that the correct sequences
    are barcoded, and that they have the correct barcodes:

    >>> bc_names = sorted([s['name'] for s in ccs_seqs if
    ...         'barcoded' in s['name']])
    >>> ccs.df = ccs.df.sort_values('barcode')
    >>> (ccs.df.query('barcoded').name == bc_names).all()
    True
    >>> barcodes = [x.split('_')[1] for x in bc_names]
    >>> (ccs.df.query('barcoded').barcode == barcodes).all()
    True
    >>> (ccs.df.query('not barcoded').barcode == ['']).all()
    True
    >>> barcode_accuracies = [float(x.split('_')[2]) for x in bc_names]
    >>> numpy.allclose(ccs.df.query('barcoded').barcode_accuracy,
    ...     barcode_accuracies, atol=1e-4)
    True
    >>> numpy.allclose(ccs.df.query('barcoded').barcode_accuracy,
    ...         [qvalsToAccuracy(qvals) for qvals in
    ...         ccs.df.query('barcoded').barcode_qvals])
    True
    >>> numpy.allclose(ccs.df.query('not barcoded').barcode_accuracy,
    ...     -1, atol=1e-4)
    True
    >>> barcoded_polarity = [{'plus':1, 'minus':-1}[x.split('_')[3]]
    ...         for x in bc_names]
    >>> (ccs.df.query('barcoded').barcoded_polarity == barcoded_polarity).all()
    True

    """

    def __init__(self, samplename, bamfile, reportfile):
        """See main class doc string."""
        self.samplename = samplename

        assert os.path.isfile(bamfile), "can't find {0}".format(bamfile)
        self.bamfile = bamfile

        self.reportfile = reportfile
        if self.reportfile is None:
            self.zmw_report = None
            self.subread_report = None
        else:
            assert os.path.isfile(reportfile), \
                    "can't find {0}".format(reportfile)
            # set `zmw_report` and `subread_report`
            self._parse_report()

        self._build_df_from_bamfile()


    def _parse_report(self):
        """Set `zmw_report` and `subread_report` using `reportfile`."""
        # match reports made by ccs 3.0.0
        reportmatch = regex.compile('^ZMW Yield\n(?P<zmw>(.+\n)+)\n\n'
                            'Subread Yield\n(?P<subread>(.+\n)+)$')

        with open(self.reportfile) as f:
            report = f.read()
        m = reportmatch.search(report)
        assert m, "Cannot match {0}\n\n{1}".format(
                self.reportfile, report)

        for read_type in ['zmw', 'subread']:
            df = (pandas.read_csv(
                        io.StringIO(m.group(read_type)),
                        names=['status', 'number', 'percent']
                        )
                  .assign(fraction=lambda x: 
                        x.percent.str.slice(None, -1)
                        .astype('float') / 100)
                  )
            setattr(self, read_type + '_report', df)


    def _build_df_from_bamfile(self):
        """Builds `df` from `bamfile`."""
        # read into dictionary
        d = collections.defaultdict(list)
        for s in pysam.AlignmentFile(self.bamfile, 'rb',
                check_sq=False):
            d['CCS'].append(s.query_sequence)
            d['CCS_qvals'].append(numpy.asarray(s.query_qualities,
                                                dtype='int'))
            d['name'].append(s.query_name)
            d['passes'].append(s.get_tag('np'))
            d['CCS_accuracy'].append(s.get_tag('rq'))
            d['CCS_length'].append(s.query_length)
            d['samplename'].append(self.samplename)

        # create data frame
        self.df = pandas.DataFrame(d)

        # some checks on `df`
        assert self.df.name.size == self.df.name.unique().size,\
                "non-unique names for {0}".format(self.name)
        assert (self.df.CCS_length == self.df.CCS.apply(len)).all(),\
                "CCS not correct length"
        assert (self.df.CCS_length == self.df.CCS_qvals.apply(len)).all(),\
                "qvals not correct length"


def matchAndAlignCCS(ccslist, mapper, *,
        termini5, gene, spacer, umi, barcode, termini3,
        targetvariants=None, mutationcaller=None,
        rc_barcode_umi=True):
    """Identify CCSs that match pattern and align them.

    This is a convenience function that runs :meth:`matchSeqs`
    and :meth:`alignSeqs` for a common use case. It takes one
    or more :class:`CCS` objects, looks for CCS sequences in them
    that match a specific pattern, and aligns them to targets. It
    returns a pandas data frame with all the results. The CCS
    sequences are assumed to be molecules that have the following
    structure, although potentially in either orientation::

        5'-...-termini5-gene-spacer-umi-barcode-termini3-...-3'

    As indicated by the ``...``, there can be sequence before and
    after our expected pattern that we ignore. The gene element
    is the aligned to the targets. The full CCS is also aligned
    in the absence of the pattern matching.

    Args:
        `ccslist` (:class:`CCS` object or list of them)
            Analyze the CCS's in the `df` attributes. If there are
            multiple :class:`CCS` objectes, they are concatenated.
            However, they must have the same columns.
        `mapper` (:py:mod:`dms_tools2.minimap2.Mapper`)
            Mapper used to perform alignments.
        `termini5` (str or `None`)
            Expected sequence at 5' end as str that can be compiled
            to `regex` object. Passed through :meth:`re_expandIUPAC`.
            For instance, make it 'ATG|CTG' if the sequence might
            start with either `ATG` or `CTG`. Set to `None` if
            no expected 5' termini. Note that we use `regex`
            rather than `re`, so fuzzy matching is enabled.
        `gene` (str)
            Like `termini5` but gives the gene to match. For instance,
            'N+' if the gene can be arbitrary sequence and length.
        `spacer` (str or `None`)
            Like `termini5`, but for the spacer after `gene`.
        `umi` (str or `None`)
            Like `termini5`, but for UMI.
        `barcode` (str or `None`)
            Like `termini5`, but for barcode. For instance, 'N{10}'
            if 10-nucleotide barcode.
        `termini3` (str or `None`)
            Like `termini5`, but for termini3.
        `targetvariants` (:class:`dms_tools2.minimap2.TargetVariants`)
            Call target variants. See docs for same argument to
            :meth:`alignSeqs`.
        `mutationcaller` (:class:`dms_tools2.minimap2.MutationCaller`)
            Call mutations. See docs for same argument to :meth:`alignSeqs`.
        `rc_barcode_umi` (bool)
            Do we reverse complement the `barcode` and `UMI` in the
            returned data frame relative to the orientation of
            the gene. Typically this is desirable because actual
            barcode sequencing goes in the reverse direction of the
            gene.

    Returns:
        A pandas dataframe that will have all columns already in the
        `df` attribute of the input :class:`CCS` objects with the
        following columns added:

        - `barcoded`: `True` if CCS matches full expected pattern,
          `False` otherwise.

        - `barcoded_polarity`: 1 of the match is in the polarity of
          the CCS, -1 if to the reverse complement, 0 if no match.

        - Columns named `termini5`, `gene`, `spacer`, `UMI`,
          `barcode`, and `termini3` (except if any of these elements
          are `None`). If `barcoded` is `True` for that CCS, these
          columns give the sequence for that element. If it is `False`,
          they are empty strings. There are likewise columns with
          these same names suffixed with "_accuracy" that give the CCS
          accuracy for that element, and columns suffixed with "_qvals"
          that give the quality scores for the elements.

        - For each of `termini5`, `spacer`, and `termini3` that are
          not `None`, a column named `has_termini5`, etc that
          indicates if that element is matched in isolate even if
          the full pattern is not matched.

        - `gene_aligned` is True if the CCS matches the expected
          pattern (is `barcoded`), and `gene` can further be
          aligned using `mapper`. It is `False` otherwise.

        - `gene_aligned_alignment`, `gene_aligned_target`,
          `gene_aligned_cigar`, `gene_aligned_n_trimmed_query_start`,
          `gene_aligned_n_trimmed_query_end`,
          `gene_aligned_n_trimmed_target_start`,
          `gene_aligned_n_trimmed_target_end`,
          `gene_aligned_n_additional`, and
          `gene_aligned_n_additional_difftarget` give the
          :py:mod:`dms_tools2.minimap2.Alignment`, the
          alignment target, the long-form CIGAR string,
          the number of nucleotides trimmed from ends of the
          the query gene or target, the number
          of additional alignments if `gene_aligned`,
          and the number of additional alignments to different
          targets (see `target_isoforms` attribute of
          :py:mod:`dms_tools2.minimap2.Mapper`). If
          the gene is not aligned, these are `None`,
          empty strings, or -1.

        - If `targetvariants` is not `None`, column named
          `gene_aligned_target_variant` giving target variant
          returned by :class:`dms_tools2.minimap2.TargtVariants.call`.

        - If `mutationcaller` is not `None`, columns named
          `gene_aligned_substitutions`, `gene_aligned_deletions`,
          and `gene_aligned_insertions` giving the specific
          mutations of each type as returned by
          :class:`dms_tools2.minimap2.MutationCaller.call`.

        - `CCS_aligned` is `True` if the CCS can be aligned
          using `mapper` even if a gene cannot be matched,
          and `False` otherwise. `CCS_aligned_alignment`
          and `CCS_aligned_target` give the
          :py:mod:`dms_tools2.minimap2.Alignment` (or `None`)
          and the target (or empty string).
    """
    if isinstance(ccslist, collections.Iterable):
        col_list = [ccs.df.columns for ccs in ccslist]
        assert all([col_list[0].equals(col) for col in col_list]),\
                "the CCS.df's in `ccslist` don't have same columns"
        df = pandas.concat([ccs.df for ccs in ccslist])
    else:
        df = ccslist.df

    # internal function:
    def _align_CCS_both_orientations(df, mapper):
        """Try align CCS both ways, adds columns.
          `CCS_aligned`, `CCS_aligned_alignment`, and
        `CCS_aligned_target`."""
        df_bi = (df.pipe(dms_tools2.pacbio.alignSeqs,
                         mapper=mapper,
                         query_col='CCS',
                         aligned_col='CCS_for_aligned')
                   .assign(CCS_rev=lambda x: x.CCS.map(
                           dms_tools2.utils.reverseComplement))
                   .pipe(dms_tools2.pacbio.alignSeqs,
                         mapper=mapper,
                         query_col='CCS_rev',
                         aligned_col='CCS_rev_aligned')
                   )
        return (df.assign(CCS_aligned=df_bi.CCS_for_aligned |
                          df_bi.CCS_rev_aligned)
                .assign(CCS_aligned_alignment=
                        df_bi.CCS_for_aligned_alignment.where(
                        df_bi.CCS_for_aligned,
                        df_bi.CCS_rev_aligned_alignment))
                .assign(CCS_aligned_target=lambda x:
                        x.CCS_aligned_alignment.map(
                        lambda x: x.target if x is not None else ''))
                )

    # build match_str
    match_str = ''
    if termini5 is not None:
        match_str += '(?P<termini5>{0})'.format(termini5)
    match_str += '(?P<gene>{0})'.format(gene)
    if spacer is not None:
        match_str += '(?P<spacer>{0})'.format(spacer)
    if umi is not None:
        match_str += '(?P<UMI>{0})'.format(umi)
    if barcode is not None:
        match_str += '(?P<barcode>{0})'.format(barcode)
    if termini3 is not None:
        match_str += '(?P<termini3>{0})'.format(termini3)

    # now create df
    df = (
        df

        # match barcoded sequences
        .pipe(dms_tools2.pacbio.matchSeqs,
              match_str=match_str,
              col_to_match='CCS',
              match_col='barcoded')
    
        # look for just termini or spacer
        .pipe(dms_tools2.pacbio.matchSeqs, 
              match_str=termini5,
              col_to_match='CCS',
              match_col='has_termini5',
              add_polarity=False,
              add_group_cols=False)
        .pipe(dms_tools2.pacbio.matchSeqs, 
              match_str=termini3,
              col_to_match='CCS',
              match_col='has_termini3',
              add_polarity=False,
              add_group_cols=False)
        .pipe(dms_tools2.pacbio.matchSeqs, 
              match_str=spacer,
              col_to_match='CCS',
              match_col='has_spacer',
              add_polarity=False,
              add_group_cols=False)
    
        # see if gene aligns in correct orientation
        .pipe(dms_tools2.pacbio.alignSeqs,
              mapper=mapper,
              query_col='gene',
              aligned_col='gene_aligned',
              targetvariants=targetvariants,
              mutationcaller=mutationcaller)
    
        # look for any alignment of CCS, take best in either orientation
        .pipe(_align_CCS_both_orientations,
              mapper=mapper)
        )

    # reverse complement barcode and UMI
    if rc_barcode_umi:
        if barcode is not None:
            df.barcode = df.barcode.map(dms_tools2.utils.reverseComplement)

        if umi is not None:
            df.UMI = df.UMI.map(dms_tools2.utils.reverseComplement)

    return df


def matchSeqs(df, match_str, col_to_match, match_col, *,
        add_polarity=True, add_group_cols=True,
        add_accuracy=True, add_qvals=True,
        expandIUPAC=True, overwrite=False):
    """Identify sequences in a dataframe that match a specific pattern.

    Args:
        `df` (pandas DataFrame)
            Data frame with column holding sequences to match.
        `match_str` (str)
            A string that can be passed to `regex.compile` that gives
            the pattern that we are looking for, with target 
            subsequences as named groups. See also the `expandIUPAC`
            parameter, which simplifies writing `match_str`.
            If `None` we just return `df`. Note that we use
            `regex` rather than `re`, so fuzzy matching is
            enabled.
        `col_to_match` (str)
            Name of column in `df` that contains the sequences
            to match.
        `match_col` (str)
            Name of column added to `df`. Elements of columns are
            `True` if `col_to_match` matches `match_str` for that
            row, and `False` otherwise.
        `add_polarity` (bool)
            Add a column specifying the polarity of the match?
        `add_group_cols` (bool)
            Add columns with the sequence of every group in
            `match_str`?
        `add_accuracy` (bool)
            For each group in the match, add a column giving
            the accuracy of that group's sequence? Only used
            if `add_group_cols` is `True`.
        `add_qvals` (bool)
            For each group in the match, add a column giving
            the Q values for that group's sequence? Only used if
            `add_group_cols` is `True`.
        `expandIUPAC` (bool)
            Use `IUPAC code <https://en.wikipedia.org/wiki/Nucleic_acid_notation>`_
            to expand ambiguous nucleotides (e.g., "N") by passing
            `match_str` through the :meth:`re_expandIUPAC` function.
        `overwrite` (bool)
            If `True`, we overwrite any existing columns to
            be created that already exist. If `False`, raise
            an error if any of the columns already exist.

    Returns:
        A **copy** of `df` with new columns added. The exact columns
        to add are specified by the calling arguments. Specifically:

            - We always add a column with the name given by `match_col`
              that is `True` if there was a match and `False` otherwise.

            - If `add_polarity` is `True`, add a column that is
              `match_col` suffixed by "_polarity" which is 1 if
              the match is directly to the sequence in `col_to_match`,
              and -1 if it is to the reverse complement of this sequence.
              The value is 0 if there is no match.

            - If `add_group_cols` is `True`, then for each group
              in `match_str` specified using the `re` group naming
              syntax, add a column with that group name that
              gives the sequence matching that group. These
              sequences are empty strings if there is no match.
              These added sequences are in the polarity of the
              match, so if the sequence in `match_col` has
              to be reverse complemented for a match, then these
              sequences will be the reverse complement that matches.
              Additionally, when `add_group_cols` is True:

                - If `add_accuracy` is `True`, we also add a column
                  suffixed by "_accuracy" that gives the
                  accuracy of that group as computed from the Q-values.
                  The value -1 if there is match for that row. Adding
                  accuracy requires a colum in `df` with the name
                  given by `match_col` suffixed by "_qvals."

                - If `add_qvals` is `True`, we also add a column 
                  suffixed by "_qvals" that gives the Q-values
                  for that sequence. Adding these Q-values requires
                  that there by a column in `df` with the name given by
                  `match_col` suffixed by "_qvals". The Q-values are
                  in the form of a numpy array, or an empty numpy array
                  if there is no match for that row.
              
    See docs for :class:`CCS` for example uses of this function.

    Here is a short example that uses the fuzzy matching of
    the `regex` model for the polyA tail:

    >>> gene = 'ATGGCT'
    >>> polyA = 'AAAACAAAA'
    >>> df = pandas.DataFrame({'CCS':[gene + polyA]})
    >>> match_str = '(?P<gene>N+)(?P<polyA>AA(A{5,}){e<=1}AA)'
    >>> df = matchSeqs(df, match_str, 'CCS', 'matched',
    ...         add_accuracy=False, add_qvals=False)
    >>> expected = df.assign(gene=gene, polyA=polyA,
    ...         matched=True, matched_polarity=1)
    >>> (df.sort_index(axis=1) == expected.sort_index(axis=1)).all().all()
    True
    """

    if match_str is None:
        return df

    assert col_to_match in df.columns, \
            "`df` lacks `col_to_match` column {0}".format(col_to_match)

    if expandIUPAC:
        match_str = re_expandIUPAC(match_str)
    matcher = regex.compile(match_str)

    newcols = [match_col]
    if add_polarity:
        polarity_col = match_col + '_polarity'
        newcols.append(polarity_col)

    if add_group_cols:
        groupnames = list(matcher.groupindex.keys())
        if len(set(groupnames)) != len(groupnames):
            raise ValueError("duplicate group names in {0}"
                             .format(match_str))
        newcols += groupnames
        if add_accuracy:
            newcols += [g + '_accuracy' for g in groupnames]
        if add_qvals:
            newcols += [g + '_qvals' for g in groupnames]
        if add_accuracy or add_qvals:
            match_qvals_col = col_to_match + '_qvals'
            if match_qvals_col not in df.columns:
                raise ValueError("To use `add_accuracy` or "
                        "`add_qvals`, you need a column in `df` "
                        "named {0}".format(match_qvals_col))
    else:
        groupnames = []

    # make sure created columns don't already exist
    dup_cols = set(newcols).intersection(set(df.columns))
    if not overwrite and dup_cols:
        raise ValueError("`df` already contains some of the "
                "columns that we are supposed to add:\n{0}"
                .format(dup_cols))

    # look for matches for each row
    match_d = {c:[] for c in newcols}
    for tup in df.itertuples():
        s = getattr(tup, col_to_match)
        m = matcher.search(s)
        if add_group_cols and (add_accuracy or add_qvals):
            qs = getattr(tup, match_qvals_col)
        if m:
            polarity = 1
        else:
            m = matcher.search(dms_tools2.utils.reverseComplement(s))
            polarity = -1
            if add_group_cols and (add_accuracy or add_qvals):
                qs = numpy.flip(qs, axis=0)
        if m:
            match_d[match_col].append(True)
            if add_polarity:
                match_d[polarity_col].append(polarity)
            for g in groupnames:
                match_d[g].append(m.group(g))
                if add_qvals:
                    match_d[g + '_qvals'].append(qs[m.start(g) : m.end(g)])
                if add_accuracy:
                    match_d[g + '_accuracy'].append(qvalsToAccuracy(
                            qs[m.start(g) : m.end(g)]))
        else:
            match_d[match_col].append(False)
            if add_polarity:
                match_d[polarity_col].append(0)
            for g in groupnames:
                match_d[g].append('')
                if add_qvals:
                    match_d[g + '_qvals'].append(numpy.array([], dtype='int'))
                if add_accuracy:
                    match_d[g + '_accuracy'].append(-1)

    # set index to make sure matches `df`
    indexname = df.index.name
    assert indexname not in match_d
    match_d[indexname] = df.index.tolist()
    if (not overwrite) and dup_cols:
        raise ValueError("overwriting columns")
    return pandas.concat(
            [df.drop(dup_cols, axis=1),
                pandas.DataFrame(match_d).set_index(indexname),
            ],
            axis=1)


def alignSeqs(df, mapper, query_col, aligned_col, *,
        add_alignment=True, add_target=True, add_cigar=True,
        add_n_trimmed=True, add_n_additional=True,
        add_n_additional_difftarget=True, targetvariants=None,
        mutationcaller=None, overwrite=True, paf_file=None):
    """Align sequences in a dataframe to target sequence(s).

    Arguments:
        `df` (pandas DataFrame)
            Data frame in which one column holds sequences to match.
            There also must be a column named "name" with unique names.
        `mapper` (:py:mod:`dms_tools2.minimap2.Mapper`)
            Align using the :py:mod:`dms_tools2.minimap2.Mapper.map`
            function of `mapper`. Target sequence(s) to which
            we align are specified when initializing `mapper`.
        `query_col` (str)
            Name of column in `df` with query sequences to align.
        `aligned_col` (str)
            Name of column added to `df`. Elements of column are
            `True` if `query_col` aligns, and `False` otherwise.
        `add_alignment` (bool)
            Add column with the :py:mod:`dms_tools2.minimap2.Alignment`.
        `add_target` (bool)
            Add column giving target (reference) to which sequence
            aligns.
        `add_cigar` (bool)
            Add column with the CIGAR string in the long format
            `described here <https://github.com/lh3/minimap2#cs>`_.
        `add_n_trimmed` (bool)
            Add columns giving number of nucleotides trimmed from
            ends of both the query and target in the alignment.
        `add_n_additional` (bool)
            Add column specifying the number of additional
            alignments.
        `targetvariants` (:class:`dms_tools2.minimap2.TargetVariants`)
            Call target variants of aligned genes using the `call`
            function of this object. Note that this also adjusts
            the returned alignments / CIGAR if a variant is called.
            If the `variantsites_min_acc` attribute is not `None`,
            then `df` must have a column with the name of `query_col`
            suffixed by '_qvals' that gives the Q-values to compute
            accuracies.
        `mutationcaller` (:class:`dms_tools2.minimap2.MutationCaller`)
            Call mutations of aligned genes using the `call` function
            of this object. Note that any target variant mutations are
            handled first and then removed and not called here.
        `add_n_additional_difftarget` (bool)
            Add columns specifying number of additional alignments
            to a target other than the one in the primary alignment.
        `overwrite` (bool)
            If `True`, we overwrite any existing columns to
            be created that already exist. If `False`, raise
            an error if any of the columns already exist.
        `paf_file` (`None` or str)
            If a str, is the name of the PAF file created
            by `mapper` (see `outfile` argument of
            :py:mod:`dms_tools2.minimap2.Mapper.map`) Otherwise
            this file is not saved.

    Returns:
        A **copy** of `df` with new columns added. The exact
        columns to add are specified by the calling arguments.
        Specifically:

            - We always add a column with the name given by
              `aligned_col` that is `True` if there was an
              alignment and `False` otherwise.
              
            - If `add_alignment` is `True`, add column named
              `aligned_col` suffixed by "_alignment" that gives
              the alignment as a :py:mod:`dms_tools2.minimap2.Alignment`
              object, or `None` if there is no alignment. Note that
              if there are multiple alignments, then this is the
              "best" alignment, and the remaining alignments are in
              the :py:mod:`dms_tools2.minimap2.Alignment.additional`
              attribute.

            - If `add_target` is `True`, add column named
              `aligned_col` suffixed by "_target" that gives
              the target to which the sequence aligns in the
              "best" alignment, or an empty string if no alignment.

            - If `add_cigar` is `True`, add column named
              `aligned_col` suffixed by "_cigar" with the CIGAR
              string (`long format <https://github.com/lh3/minimap2#cs>`_)
              for the "best" alignment, or an empty string if there
              is no alignment.

            - If `add_n_trimmed` is `True`, add column named
              `aligned_col` suffixed by "_n_trimmed_query_start",
              "_n_trimmed_query_end", "_n_trimmed_target_start",
              and "_n_trimmed_target_end" that give the number
              of nucleotides trimmed from the query and target
              in the "best" alignment. Are all zero if the
              zero if the alignment is end-to-end. Are -1 if no
              alignment.

            - If `add_n_additional` is `True`, add column
              named `aligned_col` suffixed by "_n_additional" that
              gives the number of additional alignments (in
              :py:mod:`dms_tools2.minimap2.Alignment.additional`),
              or -1 if there is no alignment.

            - If `add_n_additional_difftarget` is `True`, add column
              named `aligned_col` suffixed by "_n_additional_difftarget"
              that gives the number of additional alignments to
              **different** targets that are not isoforms, or -1
              if if there is no alignment. See the `target_isoforms`
              attribute of :py:mod:`dms_tools2.minimap2.Mapper`.

            - If `targetvariants` is not `None`, add a column
              named `aligned_col` suffixed by "_target_variant"
              that has the values returned for that alignment by
              :class:`dms_tools2.minimap2.TargetVariants.call`, or
              an empty string if no alignment.

            - If `mutationcaller` is not `None`, add columns
              named `aligned_col` suffixed by "_substitutions",
              "_insertions", and "_deletions" which give the
              mutations of each of these types in the form
              of the lists returned by 
              :class:`dms_tools2.minimap2.MutationCaller.call`,
              or an empty list if there is no alignment.
    """
    assert query_col in df.columns, "no `query_col` {0}".format(query_col)

    newcols = [aligned_col]
    if add_alignment:
        alignment_col = aligned_col + '_alignment'
        newcols.append(alignment_col)
    if add_target:
        target_col = aligned_col + '_target'
        newcols.append(target_col)
    if add_cigar:
        cigar_col = aligned_col + '_cigar'
        newcols.append(cigar_col)
    if add_n_trimmed:
        n_trimmed_prefix = aligned_col + '_n_trimmed_'
        for suffix in ['query_start', 'query_end',
                'target_start', 'target_end']:
            newcols.append(n_trimmed_prefix + suffix)
    if add_n_additional:
        n_additional_col = aligned_col + '_n_additional'
        newcols.append(n_additional_col)
    if add_n_additional_difftarget:
        n_additional_difftarget_col = (
                aligned_col + '_n_additional_difftarget')
        newcols.append(n_additional_difftarget_col)
    if targetvariants is not None:
        targetvariant_col = aligned_col + '_target_variant'
        newcols.append(targetvariant_col)
        if targetvariants.variantsites_min_acc is not None:
            qvals_col = query_col + '_qvals'
            if qvals_col not in df.columns:
                raise ValueError("Cannot use `variantsites_min_acc` "
                        "of `targetvariants` as there is not a column "
                        "in `df` named {0}".format(qvals_col))
            qvals = pandas.Series(df[qvals_col].values,
                                  index=df.name).to_dict()
    if mutationcaller is not None:
        mut_types = ['substitutions', 'insertions', 'deletions']
        newcols += ['{0}_{1}'.format(aligned_col, mut_type)
                for mut_type in mut_types]

    assert len(newcols) == len(set(newcols))

    dup_cols = set(newcols).intersection(set(df.columns))
    if (not overwrite) and dup_cols:
        raise ValueError("`df` already contains these columns:\n{0}"
                         .format(dup_cols))

    # perform the mapping
    assert len(df.name) == len(df.name.unique()), \
            "`name` in `df` not unique"
    with tempfile.NamedTemporaryFile(mode='w') as queryfile:
        queryfile.write('\n'.join([
                        '>{0}\n{1}'.format(*tup) for tup in
                        df.query('{0} != ""'.format(query_col))
                            [['name', query_col]]
                            .itertuples(index=False, name=False)
                        ]))
        map_dict = mapper.map(queryfile.name, outfile=paf_file)

    align_d = {c:[] for c in newcols}
    for name in df.name:
        if name in map_dict:
            a = map_dict[name]
            assert a.strand == 1, "method does not handle - polarity"
            if targetvariants:
                (variant, a) = targetvariants.call(a, qvals[name])
                align_d[targetvariant_col].append(variant)
            if mutationcaller:
                muts = mutationcaller.call(a)
                for mut_type in mut_types:
                    align_d['{0}_{1}'.format(aligned_col, mut_type)
                            ].append(muts[mut_type])
            align_d[aligned_col].append(True)
            if add_alignment:
                align_d[alignment_col].append(a)
            if add_target:
                align_d[target_col].append(a.target)
            if add_cigar:
                align_d[cigar_col].append(a.cigar_str)
            if add_n_trimmed:
                align_d[n_trimmed_prefix + 'query_start'].append(
                        a.q_st)
                align_d[n_trimmed_prefix + 'query_end'].append(
                        a.q_len - a.q_en)
                align_d[n_trimmed_prefix + 'target_start'].append(
                        a.r_st)
                align_d[n_trimmed_prefix + 'target_end'].append(
                        a.r_len - a.r_en)
            if add_n_additional:
                align_d[n_additional_col].append(len(a.additional))
            if add_n_additional_difftarget:
                align_d[n_additional_difftarget_col].append(
                        len([a2.target for a2 in a.additional if
                        a2.target not in mapper.target_isoforms[a.target]]))

        else:
            align_d[aligned_col].append(False)
            if add_alignment:
                align_d[alignment_col].append(None)
            if add_target:
                align_d[target_col].append('')
            if add_cigar:
                align_d[cigar_col].append('')
            if add_n_trimmed:
                for suffix in ['query_start', 'query_end',
                        'target_start', 'target_end']:
                    align_d[n_trimmed_prefix + suffix].append(-1)
            if add_n_additional:
                align_d[n_additional_col].append(-1)
            if add_n_additional_difftarget:
                align_d[n_additional_difftarget_col].append(-1)
            if targetvariants:
                align_d[targetvariant_col].append('')
            if mutationcaller:
                for mut_type in mut_types:
                    align_d['{0}_{1}'.format(aligned_col, mut_type)].append([])

    # set index to make sure matches `df`
    index_name = df.index.name
    assert index_name not in align_d
    align_d[index_name] = df.index.tolist()
    if (not overwrite) and dup_cols:
        raise ValueError("overwriting columns")
    return pandas.concat(
            [df.drop(dup_cols, axis=1),
                pandas.DataFrame(align_d).set_index(index_name),
            ],
            axis=1)


def qvalsToAccuracy(qvals, encoding='numbers'):
    """Converts set of quality scores into average accuracy.

    Args:
        `qvals` (numpy array or number or str)
            List of Q-values, assumed to be Phred scores.
            For how they are encoded, see `encoding`.
        `encoding` (str)
            If it is "numbers" then `qvals` should be a
            numpy array giving the Q-values, or a number
            with one Q-value. If it is "sanger", then `qvals`
            is a string, with the score being the ASCII value
            minus 33.

    Returns:
        A number giving the average accuracy, or 
        `nan` if `qvals` is empty.

    Note that the probability :math:`p` of an error at a
    given site is related to the Q-value :math:`Q` by
    :math:`Q = -10 \log_{10} p`.

    >>> qvals = numpy.array([13, 77, 93])
    >>> round(qvalsToAccuracy(qvals), 3)
    0.983
    >>> round(qvalsToAccuracy(qvals[1 : ]), 3)
    1.0
    >>> qvalsToAccuracy(numpy.array([]))
    nan

    >>> qvals = '.n~'
    >>> round(qvalsToAccuracy(qvals, encoding='sanger'), 3)
    0.983

    >>> round(qvalsToAccuracy(15), 3)
    0.968
    """
    if encoding == 'numbers':
        if isinstance(qvals, numbers.Number):
            qvals = numpy.array([qvals])
        elif isinstance(qvals, list):
            qvals = numpy.array(qvals)

    if len(qvals) == 0:
        return numpy.nan

    if encoding == 'numbers':
        pass
    elif encoding == 'sanger':
        qvals = numpy.array([ord(q) - 33 for q in qvals])
    else:
        raise RuntimeError("invalid `encoding`: {0}".format(encoding))

    return (1 - 10**(qvals / -10)).sum() / len(qvals)


def summarizeCCSreports(ccslist, report_type, plotfile,
                        plotminfrac=0.005):
    """Summarize and plot `CCS` reports.

    Args:
        `ccslist` (`CCS` object or list of them)
            `CCS` objects to summarize
        `report_type` (str "zmw" or "subread")
            Which type of report to summarize
        `plotfile` (str)
            Name of created bar plot
        `plotminfrac` (float)
            Only plot status categories with >=
            this fraction in at least one `CCS`

    Returns:
        Returns a pandas DataFrame aggregating the reports,
        and creates `plotfile`.
    """
    if isinstance(ccslist, CCS):
        ccslist = [ccslist]
    assert all([isinstance(ccs, CCS) for ccs in ccslist]), \
            "`ccslist` not a list of `CCS` objects"

    assert report_type in ['zmw', 'subread']
    report = report_type + '_report'

    df = (pandas.concat([getattr(ccs, report).assign(sample=ccs.samplename)
                for ccs in ccslist])
          .sort_values(['sample', 'number'], ascending=False)
          [['sample', 'status', 'number', 'fraction']]
          )

    # version of df that only has categories with `plotminfrac`
    plot_df = (df.assign(maxfrac=lambda x: x.groupby('status')
                         .fraction.transform('max'))
                 .query('maxfrac >= @plotminfrac')
                 )
    nstatus = len(plot_df.status.unique())

    p = (ggplot(plot_df) +
            geom_col(aes(x='sample', y='number', fill='status'),
                     position='stack') +
            theme(axis_text_x=element_text(angle=90, vjust=1,
                  hjust=0.5)) +
            ylab({'zmw':'ZMWs', 'subread':'subreads'}[report_type])
            )
    
    if nstatus <= len(COLOR_BLIND_PALETTE):
        p = p + scale_fill_manual(list(reversed(
                COLOR_BLIND_PALETTE[ : nstatus])))
    p.save(plotfile, 
           height=3,
           width=(2 + 0.3 * len(ccslist)),
           verbose=False)
    plt.close()

    return df

def re_expandIUPAC(re_str):
    """Expand IUPAC ambiguous nucleotide codes in `re` search string.

    Simplifies writing `re` search strings that include ambiguous
    nucleotide codes.

    Args:
        `re_str` (str)
            String appropriate to be passed to `regex.compile`.

    Returns:
        A version of `re_str` where any characters not in the group
        names that correspond to upper-case ambiguous nucleotide codes
        are expanded according to their definitions in the
        `IUPAC code <https://en.wikipedia.org/wiki/Nucleic_acid_notation>`_.

    >>> re_str = '^(?P<termini5>ATG)(?P<cDNA>N+)A+(?P<barcode>N{4})$'
    >>> re_expandIUPAC(re_str)
    '^(?P<termini5>ATG)(?P<cDNA>[ACGT]+)A+(?P<barcode>[ACGT]{4})$'
    """
    # We simply do a simple replacement on all characters not in group
    # names. So first we must find group names:
    groupname_indices = set([])
    groupname_matcher = regex.compile('\(\?P<[^>]*>')
    for m in groupname_matcher.finditer(re_str):
        for i in range(m.start(), m.end()):
            groupname_indices.add(i)
    
    # now replace ambiguous characters
    new_re_str = []
    for i, c in enumerate(re_str):
        if (i not in groupname_indices) and c in dms_tools2.NT_TO_REGEXP:
            new_re_str.append(dms_tools2.NT_TO_REGEXP[c])
        else:
            new_re_str.append(c)

    return ''.join(new_re_str)



if __name__ == '__main__':
    import doctest
    doctest.testmod()
