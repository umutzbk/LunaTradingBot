# Compute CRAN bidask 2.1.5 spread(method = "AR", sign = TRUE, na.rm = TRUE) on vectors.json -> r_output.json.
#
# Usage (run from this kit directory):  Rscript run_bidask.R [vectors.json] [r_output.json] [lib_dir]
#
# bidask 2.1.5 is installed into a kit-local library (default ./Rlib) from the pinned source tarball
# (sha256 checked), so no site library is modified. NeedsCompilation: no -> no compiler toolchain needed.
# Only dependencies: data.table (Imports) and jsonlite (this script); installed from CRAN into ./Rlib if absent.

args <- commandArgs(trailingOnly = TRUE)
vec_path <- if (length(args) >= 1) args[1] else "vectors.json"
out_path <- if (length(args) >= 2) args[2] else "r_output.json"
lib      <- if (length(args) >= 3) args[3] else "Rlib"

BIDASK_VERSION <- "2.1.5"
BIDASK_SHA256  <- "e0bf415c7e9f9859b044bf059979f811fb9dbaf68a2b41780f4b76faf5aeae9f"
BIDASK_URLS <- c(
  "https://cran.r-project.org/src/contrib/Archive/bidask/bidask_2.1.5.tar.gz",  # once 2.1.5 is superseded
  "https://cran.r-project.org/src/contrib/bidask_2.1.5.tar.gz"                   # while 2.1.5 is current
)
CRAN <- "https://cloud.r-project.org"

dir.create(lib, showWarnings = FALSE, recursive = TRUE)
.libPaths(c(normalizePath(lib), .libPaths()))

sha256_file <- function(path) {
  if (exists("sha256sum", envir = asNamespace("tools"))) return(unname(tools::sha256sum(path)))
  if (requireNamespace("digest", quietly = TRUE)) return(digest::digest(file = path, algo = "sha256"))
  if (requireNamespace("openssl", quietly = TRUE)) return(as.character(openssl::sha256(file(path))))
  stop("no sha256 implementation: need R >= 4.5 (tools::sha256sum), or the digest or openssl package")
}

for (p in c("jsonlite", "data.table")) {
  if (!requireNamespace(p, quietly = TRUE)) install.packages(p, lib = lib, repos = CRAN)
}

have <- tryCatch(as.character(packageVersion("bidask", lib.loc = lib)), error = function(e) NA_character_)
tarball_url <- NA_character_
if (is.na(have) || have != BIDASK_VERSION) {
  tb <- file.path(tempdir(), "bidask_2.1.5.tar.gz")
  for (u in BIDASK_URLS) {
    ok <- tryCatch({ suppressWarnings(download.file(u, tb, mode = "wb", quiet = TRUE)); TRUE },
                   error = function(e) FALSE)
    if (ok && file.exists(tb) && file.size(tb) > 0) { tarball_url <- u; break }
  }
  if (is.na(tarball_url)) stop("could not download bidask_2.1.5.tar.gz from: ", paste(BIDASK_URLS, collapse = ", "))
  got <- sha256_file(tb)
  if (!identical(tolower(got), BIDASK_SHA256)) stop("bidask tarball sha256 mismatch: ", got)
  install.packages(tb, lib = lib, repos = NULL, type = "source")
}
library(bidask, lib.loc = lib)
stopifnot(packageVersion("bidask") == BIDASK_VERSION)
stopifnot(normalizePath(dirname(find.package("bidask"))) == normalizePath(lib))
data.table::setDTthreads(1L)  # deterministic single-thread rolling means

# ---------------------------------------------------------------- inputs
doc <- jsonlite::fromJSON(vec_path, simplifyVector = FALSE)
num <- function(v) vapply(v, function(s) if (is.null(s)) NA_real_ else as.numeric(s), numeric(1))
hx  <- function(x) lapply(x, function(v) if (is.na(v)) NULL else sprintf("%a", v))
dc  <- function(x) lapply(x, function(v) if (is.na(v)) NULL else sprintf("%.17g", v))

ar_spread <- function(x, width) {
  s <- if (missing(width)) spread(x, method = "AR", sign = TRUE, na.rm = TRUE)
       else spread(x, width = width, method = "AR", sign = TRUE, na.rm = TRUE)
  as.numeric(s[["AR"]])
}

results <- list()
for (cs in doc$cases) {
  hi <- num(cs$high); lo <- num(cs$low); cl <- num(cs$close)
  # hex round trip self-check of this platform's parser / printf
  for (v in c(hi, lo, cl)) if (!is.na(v)) stopifnot(identical(as.numeric(sprintf("%a", v)), v))
  x <- data.frame(open = cl, high = hi, low = lo, close = cl)
  n <- nrow(x)
  wins <- list()
  for (w in cs$windows) {
    first <- as.integer(w[[1]]); last <- as.integer(w[[2]])
    stopifnot(first >= 1L, last >= first, last <= n - 1L)
    # (a) full-sample estimate (default width = nrow) on bars first-1 .. last (0-based) = rows first .. last+1
    v_full <- ar_spread(x[first:(last + 1L), , drop = FALSE])
    stopifnot(length(v_full) == 1L)
    # (b) endpoints form on the whole series: width = c(first, last + 1) (1-based endpoints); skipped when
    #     length(width) == nrow(x), where spread() would read width as per-observation widths instead
    v_ep <- NA_real_; ep_used <- FALSE
    if (n != 2L) {
      v_ep <- ar_spread(x, c(first, last + 1L)); stopifnot(length(v_ep) == 1L); ep_used <- TRUE
    }
    wins[[length(wins) + 1L]] <- list(first = first, last = last,
      full_hex = hx(v_full)[[1]], full_dec = dc(v_full)[[1]], full_is_nan = is.na(v_full),
      endpoints_used = ep_used, endpoints_hex = if (ep_used) hx(v_ep)[[1]] else NULL,
      endpoints_is_nan = if (ep_used) is.na(v_ep) else NULL)
  }
  roll <- NULL
  if (!is.null(cs$rolling_width)) {
    wd <- as.integer(cs$rolling_width)
    v <- ar_spread(x, wd)
    stopifnot(length(v) == n - wd + 1L)
    # output row r (0-based) = wd-1 .. n-1  <->  ar_window(first = r - wd + 2, last = r)
    roll <- list(width = wd, first_row = wd - 1L, values_hex = hx(v))
  }
  results[[length(results) + 1L]] <- list(name = cs$name, n = n, windows = wins, rolling = roll)
}

out <- list(
  schema = "luna-v7-ar-r-output/1",
  call = 'bidask::spread(x, width, method = "AR", sign = TRUE, na.rm = TRUE)',
  created_utc = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  vectors_sha256 = sha256_file(vec_path),
  r_version = R.version.string,
  bidask_version = as.character(packageVersion("bidask")),
  bidask_tarball_sha256 = BIDASK_SHA256,
  bidask_tarball_url = if (is.na(tarball_url)) "already installed in lib (sha256 checked at install)" else tarball_url,
  data_table_version = as.character(packageVersion("data.table")),
  jsonlite_version = as.character(packageVersion("jsonlite")),
  platform = R.version$platform,
  sysinfo = as.list(Sys.info()[c("sysname", "release", "machine")]),
  session_info = capture.output(sessionInfo()),
  results = results
)
jsonlite::write_json(out, out_path, auto_unbox = TRUE, pretty = TRUE, null = "null", na = "null", digits = NA)
cat(sprintf("wrote %s: %d cases, bidask %s, %s\n", out_path, length(results),
            as.character(packageVersion("bidask")), R.version.string))
