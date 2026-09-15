# sky130-verify — .mag -> .gds conversion for the KLayout cross-check
# (--with-klayout), which doesn't read Magic's native format.
#
# Input: SKY130VERIFY_CELL, loaded from work_dir/<cell>.mag. Output:
# converted.gds in the cwd.

set cell $env(SKY130VERIFY_CELL)

if {[catch {
    load $cell
} err]} {
    puts "GDSWRITE_LOAD_FAIL $err"
    quit -noprompt
}
puts "GDSWRITE_LOAD_OK"

if {[catch {
    gds write converted.gds
} err]} {
    puts "GDSWRITE_FAIL $err"
    quit -noprompt
}
puts "GDSWRITE_OK"
quit -noprompt
