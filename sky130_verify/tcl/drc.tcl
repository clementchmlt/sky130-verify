# sky130-verify: Magic DRC of a single cell.
#
# Input (environment variables):
#   SKY130VERIFY_VIEW    layout view path (for .mag, already copied under the
#                        cell name; see toolchain.run_extraction)
#   SKY130VERIFY_FORMAT  mag | gds
#   SKY130VERIFY_CELL    name of the cell to load
#
# Output on stdout: DRC_LOAD_OK|DRC_LOAD_FAIL, then DRC_RUN_OK|DRC_RUN_FAIL.
# `drc count total` prints "Total DRC errors found: N"; the Python caller
# parses that line, because the Tcl return value is not reliable.

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
    puts "DRC_LOAD_FAIL $err"
    quit -noprompt
}

puts "DRC_LOAD_OK"

if {[catch {
    select top cell
    drc check
    drc catchup
    drc count total
} err]} {
    puts "DRC_RUN_FAIL $err"
    quit -noprompt
}

puts "DRC_RUN_OK"
quit -noprompt
