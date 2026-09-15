# sky130-verify — geometric DRC of a single cell.
#
# Input (env): SKY130VERIFY_VIEW, SKY130VERIFY_FORMAT (mag|gds),
# SKY130VERIFY_CELL. Output: DRC_LOAD_OK|FAIL then DRC_RUN_OK|FAIL; error
# count is parsed by the caller from "Total DRC errors found: N".

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
