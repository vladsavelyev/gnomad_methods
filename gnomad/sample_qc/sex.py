# noqa: D100

import logging
from typing import Tuple

import hail as hl

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SEXES = {"Male": "Male", "Female": "Female"}


def adjusted_sex_ploidy_expr(
    locus_expr: hl.expr.LocusExpression,
    gt_expr: hl.expr.CallExpression,
    karyotype_expr: hl.expr.StringExpression,
    xy_karyotype_str: str = "XY",
    xx_karyotype_str: str = "XX",
) -> hl.expr.CallExpression:
    """
    Create an entry expression to convert males to haploid on non-PAR X/Y and females to missing on Y.

    :param locus_expr: Locus
    :param gt_expr: Genotype
    :param karyotype_expr: Karyotype
    :param xy_karyotype_str: Male sex karyotype representation
    :param xx_karyotype_str: Female sex karyotype representation
    :return: Genotype adjusted for sex ploidy
    """
    male = karyotype_expr == xy_karyotype_str
    female = karyotype_expr == xx_karyotype_str
    x_nonpar = locus_expr.in_x_nonpar()
    y_par = locus_expr.in_y_par()
    y_nonpar = locus_expr.in_y_nonpar()
    return (
        hl.case(missing_false=True)
        .when(female & (y_par | y_nonpar), hl.null(hl.tcall))
        .when(male & (x_nonpar | y_nonpar) & gt_expr.is_het(), hl.null(hl.tcall))
        .when(male & (x_nonpar | y_nonpar), hl.call(gt_expr[0], phased=False))
        .default(gt_expr)
    )


def adjust_sex_ploidy(
    mt: hl.MatrixTable,
    sex_expr: hl.expr.StringExpression,
    male_str: str = "male",
    female_str: str = "female",
) -> hl.MatrixTable:
    """
    Convert males to haploid on non-PAR X/Y, sets females to missing on Y.

    :param mt: Input MatrixTable
    :param sex_expr: Expression pointing to sex in MT (if not male_str or female_str, no change)
    :param male_str: String for males (default 'male')
    :param female_str: String for females (default 'female')
    :return: MatrixTable with fixed ploidy for sex chromosomes
    """
    return mt.annotate_entries(
        GT=adjusted_sex_ploidy_expr(mt.locus, mt.GT, sex_expr, male_str, female_str)
    )


def get_ploidy_cutoffs(
    ht: hl.Table,
    f_stat_cutoff: float,
    normal_ploidy_cutoff: int = 5,
    aneuploidy_cutoff: int = 6,
) -> Tuple[Tuple[float, Tuple[float, float], float], Tuple[Tuple[float, float], float]]:
    """
    Get chromosome X and Y ploidy cutoffs for XY and XX samples.

    .. note::

        This assumes the input hail Table has the fields f_stat, chrX_ploidy, and chrY_ploidy.

    Return a tuple of sex chromosome ploidy cutoffs: ((x_ploidy_cutoffs), (y_ploidy_cutoffs)).
    x_ploidy_cutoffs: (upper cutoff for single X, (lower cutoff for double X, upper cutoff for double X), lower cutoff for triple X)
    y_ploidy_cutoffs: ((lower cutoff for single Y, upper cutoff for single Y), lower cutoff for double Y)

    Uses the normal_ploidy_cutoff parameter to determine the ploidy cutoffs for XX and XY karyotypes.
    Uses the aneuploidy_cutoff parameter to determine the cutoffs for sex aneuploidies.

    Note that f-stat is used only to split the samples into roughly 'XX' and 'XY' categories and is not used in the final karyotype annotation.

    :param ht: Table with f_stat and sex chromosome ploidies
    :param f_stat_cutoff: f-stat to roughly divide 'XX' from 'XY' samples. Assumes XX samples are below cutoff and XY are above cutoff.
    :param normal_ploidy_cutoff: Number of standard deviations to use when determining sex chromosome ploidy cutoffs for XX, XY karyotypes.
    :param aneuploidy_cutoff: Number of standard deviations to use when sex chromosome ploidy cutoffs for aneuploidies.
    :return: Tuple of ploidy cutoff tuples: ((x_ploidy_cutoffs), (y_ploidy_cutoffs))
    """
    normal_ploidy_cutoff = normal_ploidy_cutoff or 5
    aneuploidy_cutoff = aneuploidy_cutoff or 6

    # Group sex chromosome ploidy table by f_stat cutoff and get mean/stdev for chrX/Y ploidies
    sex_stats = ht.aggregate(
        hl.agg.group_by(
            hl.cond(ht.f_stat < f_stat_cutoff, "xx", "xy"),
            hl.struct(x=hl.agg.stats(ht.chrX_ploidy), y=hl.agg.stats(ht.chrY_ploidy)),
        )
    )
    logger.info("XX stats: %s", sex_stats["xx"])
    logger.info("XY stats: %s", sex_stats["xy"])

    cutoffs = (
        (
            # 1.09                 + 5                     * 0.05                      = 1.3490061717776771
            sex_stats["xy"].x.mean + (normal_ploidy_cutoff * sex_stats["xy"].x.stdev),  # upper_cutoff_X
            (
                # 2.16                 - 1.289 = 5             * 0.25                       = 1.609445224376911
                sex_stats["xx"].x.mean - (normal_ploidy_cutoff * sex_stats["xx"].x.stdev),  # lower_cutoff_XX
                # 2.16                 + 1.289 = 5             * 0.25                       = 2.1693112584404615
                sex_stats["xx"].x.mean + (normal_ploidy_cutoff * sex_stats["xx"].x.stdev),  # upper_cutoff_XX
            ),
            # 2.16                 +  6                 * 0.257                      = 2.2252978618468164
            sex_stats["xx"].x.mean + (aneuploidy_cutoff * sex_stats["xx"].x.stdev),  # lower_cutoff_XXX
        ),
        (
            (
                # 0.28                 + 5                     * 0.15                       = 1.0812723372651134
                sex_stats["xx"].y.mean + (normal_ploidy_cutoff * sex_stats["xx"].y.stdev),  # lower_cutoff_Y
                # 0.92                 + 5                     * 0.06                       = 1.2390433418143896
                sex_stats["xy"].y.mean + (normal_ploidy_cutoff * sex_stats["xy"].y.stdev),  # upper_cutoff_Y
            ),
            # 1.78                 + 1.27                                            = 1.302499243854204
            sex_stats["xy"].y.mean + (aneuploidy_cutoff * sex_stats["xy"].y.stdev),  # lower_cutoff_YY
        ),
    )

    logger.info("X ploidy cutoffs: %s", cutoffs[0])
    logger.info("Y ploidy cutoffs: %s", cutoffs[1])
    return cutoffs


def get_sex_expr(
    chr_x_ploidy: hl.expr.NumericExpression,
    chr_y_ploidy: hl.expr.NumericExpression,
    x_ploidy_cutoffs: Tuple[float, Tuple[float, float], float],
    y_ploidy_cutoffs: Tuple[Tuple[float, float], float],
) -> hl.expr.StructExpression:
    """
    Create a struct with X_karyotype, Y_karyotype, and sex_karyotype.

    Note that X0 is currently returned as 'X'.

    :param chr_x_ploidy: Chromosome X ploidy (or relative ploidy)
    :param chr_y_ploidy: Chromosome Y ploidy (or relative ploidy)
    :param x_ploidy_cutoffs: Tuple of X chromosome ploidy cutoffs: (upper cutoff for single X, (lower cutoff for double X, upper cutoff for double X), lower cutoff for triple X)
    :param y_ploidy_cutoffs: Tuple of Y chromosome ploidy cutoffs: ((lower cutoff for single Y, upper cutoff for single Y), lower cutoff for double Y)
    :return: Struct containing X_karyotype, Y_karyotype, and sex_karyotype
    """
    sex_expr = hl.struct(
        X_karyotype=(
            hl.case()
            .when(chr_x_ploidy < x_ploidy_cutoffs[0], "X")
            .when(
                (
                    (chr_x_ploidy > x_ploidy_cutoffs[1][0])
                    & (chr_x_ploidy < x_ploidy_cutoffs[1][1])
                ),
                "XX",
            )
            .when((chr_x_ploidy >= x_ploidy_cutoffs[2]), "XXX")
            .default("ambiguous")
        ),
        Y_karyotype=(
            hl.case()
            .when(chr_y_ploidy < y_ploidy_cutoffs[0][0], "")
            .when(
                (
                    (chr_y_ploidy > y_ploidy_cutoffs[0][0])
                    & (chr_y_ploidy < y_ploidy_cutoffs[0][1])
                ),
                "Y",
            )
            .when(chr_y_ploidy >= y_ploidy_cutoffs[1], "YY")
            .default("ambiguous")
        ),
    )

    return sex_expr.annotate(
        sex_karyotype=hl.if_else(
            (sex_expr.X_karyotype == "ambiguous")
            | (sex_expr.Y_karyotype == "ambiguous"),
            "ambiguous",
            sex_expr.X_karyotype + sex_expr.Y_karyotype,
        )
    )
