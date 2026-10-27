**To:** nodd@noaa.gov
**Subject:** GFS 0.25° archive (noaa-gfs-bdp-pds): stale .idx files for 2022-11-29 12Z to 2022-11-30 06Z

**Attachments:** affected-files.csv, gfs-20221129-30-corrected-idx.tar.gz

---

Hello NODD team,

I found 196 GRIB2 files in the GFS bucket on AWS (noaa-gfs-bdp-pds) whose .idx inventories do not match the GRIB data. The mirror on Google Cloud (global-forecast-system) has the same problem. All affected files are pgrb2.0p25 forecast steps of four cycles around the GFS v16.3.0 implementation:

| Cycle | Affected steps |
|---|---|
| gfs.20221129/12 | 17 |
| gfs.20221129/18 | 58 |
| gfs.20221130/00 | 68 |
| gfs.20221130/06 | 53 |

It looks as if the GRIB files were re-uploaded several hours later while the matching .idx files were left as they were. Example `gfs.20221130/00/atmos/gfs.t00z.pgrb2.0p25.f351`:

- GRIB: Last-Modified 2022-11-30 15:25:58 GMT, 540,088,141 bytes
- .idx: Last-Modified 2022-11-30 05:04:06 GMT, last offset 543,263,318 (beyond the end of the file)

The GRIB file still contains 743 messages in the same order as the inventory, but with different lengths. From the second message on, every offset is wrong. Partial downloads with HTTP byte ranges, as recommended for this bucket, therefore return fragments of messages. In 93 of the 196 files, the last offset even lies beyond the end of the file.

The attached CSV lists every affected file with the Last-Modified dates of the GRIB and the .idx, the GRIB size, and the last offset in the published inventory and in the corrected one.

The archive contains corrected .idx files for all 196 files, in the same directory layout as the bucket. I generated them by scanning the section 0 headers of each GRIB file (message lengths) and keeping the inventory lines of the published .idx in their original order, after checking that the number of messages matches. I spot-checked 588 messages from the corrected inventories with HTTP range requests: every one starts with "GRIB", ends with "7777", and matches its inventory line in forecast hour. Regenerating the files with wgrib2 -s on your side should give the same offsets.

Could you replace the .idx files in both buckets? Thank you for providing the archive. We use it to build an open weather-based forecast for the German electricity market.

Best regards,
Daniel Glaser
https://github.com/the78mole/PredictionDB
