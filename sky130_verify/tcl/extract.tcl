# sky130-verify: extraction of a single cell to SPICE.
#
# environment variables (see tcl/drc.tcl).
#
# Input:
#   SKY130VERIFY_VIEW    layout view path
#   SKY130VERIFY_FORMAT  mag | gds
#   SKY130VERIFY_CELL    cell name
#
# `ext2spice -o` is not available across all Magic 8.3 releases, so ext2spice
# writes its default <cell>.spice in the working directory, where the Python
# caller reads it.

set view   $env(SKY130VERIFY_VIEW)
set format $env(SKY130VERIFY_FORMAT)
set cell   $env(SKY130VERIFY_CELL)

if {[catch {
    if {$format eq "gds"} {
        gds readonly true
        gds rescale false
        gds read $view
        load $cell
    } else {
        load $cell
    }
} err]} {
    puts "EXTRACT_LOAD_FAIL $err"
    quit -noprompt
}

puts "EXTRACT_LOAD_OK"

if {[catch {
    select top cell
    extract do local
    extract all
    ext2spice lvs
    ext2spice
} err]} {
    puts "EXTRACT_RUN_FAIL $err"
    quit -noprompt
}

puts "EXTRACT_RUN_OK"
quit -noprompt
