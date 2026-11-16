import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

/** Eine Zeitreihe: Anzeigename plus [Zeit, Wert]-Punkte (Zeit in Sekunden). */
export interface Zeitreihe {
  label: string;
  points: [number, number][];
}

/** Props des Diagramms (eingefroren): Titel, Einheit und die Zeitreihen. */
export interface TimeChartProps {
  title: string;
  unit: string;
  series: Zeitreihe[];
}

/** Feste Diagrammhoehe in CSS-Pixeln; die Breite kommt vom Behaelter. */
const HOEHE = 220;

/** Farbe fuer Achsen und Gitter (entspricht den CSS-Variablen des Dark Themes). */
const ACHSE_FARBE = "#9aa3af";
const GITTER_FARBE = "#262d38";

/** Linienfarben fuer bis zu vier Serien (Dark Theme). */
const SERIEN_FARBEN = ["#4fc1ff", "#e5c07b", "#3fb950", "#f85149"];

/** Breite der y-Achse; genug Platz fuer kompakte Millionen-/Milliardenwerte. */
const Y_ACHSE_BREITE = 74;

const achsenFormat = new Intl.NumberFormat("de-DE", {
  maximumFractionDigits: 1,
});

/**
 * Kompakte y-Achsen-Beschriftung: grosse Token-Summen werden zu "1,8 Mio.",
 * damit sie nicht aus der Achsenbreite herauslaufen.
 */
function achsenWert(wert: number): string {
  const betrag = Math.abs(wert);
  if (betrag >= 1e9) return `${achsenFormat.format(wert / 1e9)} Mrd.`;
  if (betrag >= 1e6) return `${achsenFormat.format(wert / 1e6)} Mio.`;
  return achsenFormat.format(wert);
}

/**
 * Bringt die Punktreihen auf eine gemeinsame x-Achse (``uPlot.AlignedData``).
 * Zeitpunkte ohne Messwert einer Reihe werden zu ``null`` (Luecke im Chart).
 */
function ausrichten(series: Zeitreihe[]): uPlot.AlignedData {
  const zeiten = Array.from(
    new Set(series.flatMap((reihe) => reihe.points.map((punkt) => punkt[0]))),
  ).sort((a, b) => a - b);
  const werte = series.map((reihe) => {
    const nachZeit = new Map(reihe.points);
    return zeiten.map((zeit) => nachZeit.get(zeit) ?? null);
  });
  const daten: uPlot.AlignedData = [zeiten, ...werte];
  return daten;
}

/**
 * uPlot-Wrapper: baut das Diagramm beim Mounten auf, aktualisiert es bei
 * neuen Daten (Effect-Abhaengigkeiten) und zerstoert es im Cleanup. Ein
 * ResizeObserver haelt die Breite am Behaelter.
 */
export default function TimeChart({ title, unit, series }: TimeChartProps) {
  const behaelter = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = behaelter.current;
    if (element === null) {
      return;
    }

    const optionen: uPlot.Options = {
      title,
      width: element.clientWidth > 0 ? element.clientWidth : 600,
      height: HOEHE,
      scales: { x: { time: true } },
      series: [
        { label: "Zeit" },
        ...series.map((reihe, index) => ({
          label: `${reihe.label} (${unit})`,
          stroke: SERIEN_FARBEN[index % SERIEN_FARBEN.length],
          width: 2,
          points: { show: true, size: 5 },
        })),
      ],
      axes: [
        { stroke: ACHSE_FARBE, grid: { stroke: GITTER_FARBE } },
        {
          stroke: ACHSE_FARBE,
          grid: { stroke: GITTER_FARBE },
          size: Y_ACHSE_BREITE,
          values: (_u, splits) =>
            splits.map((wert) => (wert == null ? "" : achsenWert(wert))),
        },
      ],
    };
    const diagramm = new uPlot(optionen, ausrichten(series), element);

    const beobachter = new ResizeObserver(() => {
      const breite = element.clientWidth;
      if (breite > 0 && breite !== diagramm.width) {
        diagramm.setSize({ width: breite, height: HOEHE });
      }
    });
    beobachter.observe(element);

    return () => {
      beobachter.disconnect();
      diagramm.destroy();
    };
  }, [title, unit, series]);

  return <div className="zeit-diagramm" ref={behaelter} />;
}
